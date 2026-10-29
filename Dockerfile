FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RB_VAULT_PATH=/vault \
    RB_DATA_PATH=/data \
    RB_PORT=8080

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && mkdir -p /vault /data \
    && chmod 0777 /data

# The container is meant to run as the user that owns the notes (compose: `user: "PUID:PGID"`).
USER 1000:1000

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os,sys,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('RB_PORT','8080')+'/health',timeout=4); sys.exit(0 if r.read()==b'ok' else 1)"

ENTRYPOINT ["remote-brain-mcp"]
