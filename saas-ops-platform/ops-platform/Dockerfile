# Ops platform: monitoring agent + automation + REST API + dashboard
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --system ops && useradd --system --gid ops --home /app ops

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY ops ./ops
COPY config ./config

USER ops
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)" || exit 1

# One worker on purpose: the monitoring loop runs in-process and must not be duplicated.
# Scale the API separately with OPS_RUN_AGENT=false replicas if needed.
CMD ["uvicorn", "ops.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-access-log"]
