import json

import pytest

from ops.config import LogRule
from ops.logs import DockerLogSource, FileLogSource, _norm_ts, analyze, fingerprint, parse_line


def test_fingerprint_collapses_variable_parts():
    a = fingerprint("database timeout after 3012ms on query orders_by_user user_id=8812")
    b = fingerprint("database timeout after 7741ms on query orders_by_user user_id=113")
    c = fingerprint("connection pool exhausted (size=20, waiting=4)")
    assert a == b
    assert a != c
    assert "<*>" in a[1] and "orders_by_user" in a[1]


def test_fingerprint_handles_uuids_ips_and_quotes():
    fp1, t = fingerprint("user 3f1c9a2e-1b2c-4d5e-8f90-123456789abc from 10.0.0.12 not found: 'alice'")
    fp2, _ = fingerprint("user 00000000-1111-2222-3333-444444444444 from 192.168.1.1 not found: 'bob'")
    assert fp1 == fp2
    assert "<uuid>" in t and "<ip>" in t and "<str>" in t


def test_parse_json_and_plaintext_levels():
    j = parse_line(json.dumps({"level": "error", "msg": "boom"}))
    assert j.level == "ERROR" and j.message == "boom" and j.is_error
    pg = parse_line("2026-10-07 08:00:00.123 UTC [1] FATAL:  terminating connection due to administrator command")
    assert pg.level == "FATAL" and pg.is_error
    plain = parse_line("everything is fine")
    assert plain.level == "INFO" and not plain.is_error
    assert parse_line("   ") is None


def _access(status, ms, route="/api/v1/orders"):
    level = "ERROR" if status >= 500 else "INFO"
    return json.dumps(
        {"level": level, "msg": "request", "method": "GET", "route": route, "status": status, "duration_ms": ms}
    )


def test_analyze_derives_api_stats_from_access_logs():
    lines = [_access(200, 20)] * 18 + [_access(500, 900)] * 2
    w = analyze(lines, [], window_s=60)
    assert w.api_requests == 20
    assert w.api_5xx == 2
    assert w.api_error_ratio == pytest.approx(0.1)
    assert w.api_p95_ms == 900
    assert w.errors == 2
    # 5xx access lines share one signature per route
    assert len(w.signatures) == 1
    assert w.top_signatures()[0].template.startswith("HTTP <*> on GET")


def test_analyze_rules_and_error_rate():
    rule = LogRule(name="db", pattern="database timeout", severity="warning", min_count=2)
    lines = [json.dumps({"level": "ERROR", "msg": f"database timeout after {i}ms"}) for i in range(30)]
    w = analyze(lines, [rule], window_s=30)
    assert w.rule_hits["db"] == 30
    assert w.errors_per_min == pytest.approx(60)
    assert len(w.signatures) == 1 and w.top_signatures()[0].count == 30


def test_norm_ts_orders_trimmed_nanoseconds():
    # Docker trims trailing zeros: .5895 is earlier than .58951
    assert _norm_ts("2026-10-07T08:58:19.5895Z") < _norm_ts("2026-10-07T08:58:19.58951Z")
    assert _norm_ts("2026-10-07T08:58:19Z") < _norm_ts("2026-10-07T08:58:19.000000001Z")


async def test_docker_source_deduplicates_overlap():
    class RT:
        calls = 0

        async def logs_since(self, container, since):
            self.calls += 1
            if self.calls == 1:
                return ["2026-10-07T08:00:00.1Z a", "2026-10-07T08:00:00.2Z b"]
            return ["2026-10-07T08:00:00.2Z b", "2026-10-07T08:00:01Z c"]  # overlap re-sends "b"

    src = DockerLogSource(RT(), "x")
    assert await src.read() == ["a", "b"]
    assert await src.read() == ["c"]


async def test_file_source_tails_and_handles_truncation(tmp_path):
    f = tmp_path / "svc.log"
    f.write_text("old history\n")
    src = FileLogSource(str(f))
    assert await src.read() == []  # starts at the end: history doesn't raise incidents
    with f.open("a") as fh:
        fh.write("one\ntwo\n")
    assert await src.read() == ["one", "two"]
    with f.open("a") as fh:
        fh.write("three\npart")
    assert await src.read() == ["three"]  # partial line held back
    with f.open("a") as fh:
        fh.write("ial\n")
    assert await src.read() == ["partial"]
    f.write_text("new\n")  # truncated / rotated
    assert await src.read() == ["new"]
