FROM python:3.11-slim

# ── System dependencies ────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq-dev \
        gcc \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ── Working directory ──────────────────────────────────────────
WORKDIR /app

# ── Python dependencies (layer-cached unless requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ───────────────────────────────────────────
COPY . .

# ── Create runtime directories ─────────────────────────────────
RUN mkdir -p logs auth ml/models data

# ── Environment defaults (override via docker-compose / .env) ──
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ENV=docker \
    LOG_DIR=/app/logs \
    DATABASE_URL=postgresql://trading:trading@db:5432/trading

# ── Ports ──────────────────────────────────────────────────────
EXPOSE 8000

# ── Entrypoint: run migrations then start the API server ───────
CMD ["sh", "-c", "python db/migrate.py && uvicorn dashboard.main:app --host 0.0.0.0 --port 8000 --workers 4 --timeout-keep-alive 30"]
