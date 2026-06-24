FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# PYTHONUNBUFFERED so the first-run login URL is flushed to the logs immediately.
# Tokens persist in a volume (no hardcoded host path).
ENV PYTHONUNBUFFERED=1 \
    TOKEN_FILE=/app/data/tokens.json
EXPOSE 5001 1455

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PROXY_PORT:-5001}"]
