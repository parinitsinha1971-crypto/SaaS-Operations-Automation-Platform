# Simulated SaaS service image. One image, two roles (SERVICE_ROLE=api|web),
# plus the traffic generator (python -m sim.traffic).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --system app && useradd --system --gid app --home /app app \
    && mkdir -p /data /var/log/saas && chown app:app /data /var/log/saas

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY sim ./sim

USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2)" || exit 1

CMD ["uvicorn", "sim.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
