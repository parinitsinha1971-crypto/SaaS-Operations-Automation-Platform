"""Log collection and analysis.

Sources
  DockerLogSource  reads container stdout/stderr through the Docker API
  FileLogSource    tails a log file (handles truncation/rotation)

Analysis
  * levels are read from JSON logs, or matched in plain text (Postgres, nginx...)
  * error messages are fingerprinted (ids, numbers, hex, quoted values -> <*>)
    so 500 "timeout after 3012ms user_id=8812" lines collapse into one signature
  * JSON access logs ({"msg": "request", "status", "duration_ms"}) yield
    request count, 5xx ratio and p95 latency - API monitoring from logs alone
  * configurable regex rules (OOM, connection refused, ...) raise their own incidents
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .config import LogRule
from .db import utcnow
from .runtime import Runtime

ERROR_LEVELS = {"ERROR", "CRITICAL", "FATAL", "PANIC"}
_LEVEL_RE = re.compile(r"\b(DEBUG|INFO|NOTICE|LOG|WARN|WARNING|ERROR|FATAL|PANIC|CRITICAL)\b")
_DOCKER_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s?(.*)$")

_NORMALIZERS = [
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I), "<uuid>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ][\d:.]+Z?\b"), "<ts>"),
    (re.compile(r"\b\d+(\.\d+){3}(:\d+)?\b"), "<ip>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<hex>"),
    (re.compile(r"'[^']*'|\"[^\"]*\""), "<str>"),
    (re.compile(r"\b\d[\w-]*\b"), "<*>"),  # numbers and number+unit: 20, 3012ms
    # longer tokens containing a digit (ids, hashes); short ones like "v1" or "s3" are kept
    (re.compile(r"\b(?=[\w-]*\d)[\w-]{4,}\b"), "<*>"),
]


def fingerprint(message: str) -> tuple[str, str]:
    template = message.strip()
    for rx, repl in _NORMALIZERS:
        template = rx.sub(repl, template)
    template = re.sub(r"\s+", " ", template)[:300]
    return hashlib.sha1(template.encode(), usedforsecurity=False).hexdigest()[:16], template


@dataclass
class LogEntry:
    level: str
    message: str
    raw: str
    fields: dict = field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        return self.level in ERROR_LEVELS

    @property
    def is_access(self) -> bool:
        return self.fields.get("msg") == "request" and "status" in self.fields


def parse_line(line: str) -> LogEntry | None:
    line = line.strip()
    if not line:
        return None
    if line.startswith("{"):
        try:
            data = json.loads(line)
            level = str(data.get("level", "INFO")).upper()
            return LogEntry("WARNING" if level == "WARN" else level, str(data.get("msg", "")), line, data)
        except ValueError:
            pass
    m = _LEVEL_RE.search(line)
    level = m.group(1) if m else "INFO"
    level = {"WARN": "WARNING", "LOG": "INFO", "NOTICE": "INFO"}.get(level, level)
    message = line[m.end() :].lstrip(" :]") if m else line
    return LogEntry(level, message, line)


@dataclass
class Signature:
    fingerprint: str
    template: str
    sample: str
    level: str
    count: int = 0


@dataclass
class LogWindow:
    window_s: float
    lines: int = 0
    by_level: Counter = field(default_factory=Counter)
    errors: int = 0
    signatures: dict[str, Signature] = field(default_factory=dict)
    rule_hits: dict[str, int] = field(default_factory=dict)
    rule_samples: dict[str, str] = field(default_factory=dict)
    api_requests: int = 0
    api_5xx: int = 0
    api_durations_ms: list[float] = field(default_factory=list)

    @property
    def errors_per_min(self) -> float:
        return self.errors * 60.0 / self.window_s if self.window_s > 0 else 0.0

    @property
    def api_error_ratio(self) -> float | None:
        return self.api_5xx / self.api_requests if self.api_requests else None

    @property
    def api_p95_ms(self) -> float | None:
        if not self.api_durations_ms:
            return None
        ordered = sorted(self.api_durations_ms)
        return ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]

    def top_signatures(self, n: int = 5) -> list[Signature]:
        return sorted(self.signatures.values(), key=lambda s: s.count, reverse=True)[:n]


def analyze(lines: list[str], rules: list[LogRule], window_s: float) -> LogWindow:
    win = LogWindow(window_s=window_s)
    compiled = [(r, re.compile(r.pattern, re.I)) for r in rules]
    for line in lines:
        entry = parse_line(line)
        if entry is None:
            continue
        win.lines += 1
        win.by_level[entry.level] += 1

        if entry.is_access:
            win.api_requests += 1
            status = int(entry.fields.get("status", 0))
            if status >= 500:
                win.api_5xx += 1
            if "duration_ms" in entry.fields:
                win.api_durations_ms.append(float(entry.fields["duration_ms"]))

        if entry.is_error:
            win.errors += 1
            if entry.is_access:
                f = entry.fields
                msg = f"HTTP {f.get('status')} on {f.get('method', '')} {f.get('route', '')}"
            else:
                msg = entry.message or entry.raw
            fp, template = fingerprint(msg)
            sig = win.signatures.setdefault(fp, Signature(fp, template, msg[:500], entry.level))
            sig.count += 1

        for rule, rx in compiled:
            if rx.search(entry.raw):
                win.rule_hits[rule.name] = win.rule_hits.get(rule.name, 0) + 1
                win.rule_samples.setdefault(rule.name, entry.raw[:500])
    return win


def _norm_ts(ts: str) -> str:
    """Docker trims trailing zeros (RFC3339Nano); pad to 9 digits so text order == time order."""
    base, _, frac = ts.rstrip("Z").partition(".")
    return f"{base}.{frac.ljust(9, '0')[:9]}Z"


class DockerLogSource:
    def __init__(self, runtime: Runtime, container: str) -> None:
        self.runtime = runtime
        self.container = container
        self.last_ts: str | None = None  # RFC3339Nano of the last line we consumed
        self.since = utcnow() - timedelta(seconds=30)

    async def read(self) -> list[str]:
        raw = await self.runtime.logs_since(self.container, self.since)
        self.since = utcnow() - timedelta(seconds=2)  # small overlap, de-duplicated below
        out = []
        for line in raw:
            m = _DOCKER_TS_RE.match(line)
            if not m:
                out.append(line)
                continue
            ts, msg = m.groups()
            ts = _norm_ts(ts)
            if self.last_ts and ts <= self.last_ts:
                continue
            self.last_ts = ts
            out.append(msg)
        return out


class FileLogSource:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.offset: int | None = None

    async def read(self) -> list[str]:
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        if self.offset is None:
            self.offset = size  # start at the end: history must not raise fresh incidents
        if size < self.offset:  # truncated or rotated
            self.offset = 0
        with self.path.open("rb") as fh:
            fh.seek(self.offset)
            data = fh.read(4 * 1024 * 1024)
            self.offset = fh.tell()
        text = data.decode("utf-8", "replace")
        if not text.endswith("\n") and "\n" in text:
            # keep a partial last line for the next read
            keep = text[text.rfind("\n") + 1 :]
            self.offset -= len(keep.encode())
            text = text[: text.rfind("\n") + 1]
        return text.splitlines()


class NullLogSource:
    async def read(self) -> list[str]:
        return []


def window_seconds(prev: datetime | None, now: datetime, default: float) -> float:
    return max(1.0, (now - prev).total_seconds()) if prev else default
