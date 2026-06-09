# Single long-lived container running the in-process APScheduler (`serve`).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first for better layer caching.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install .

# state.db lives on a mounted persistent volume in production.
ENV STATE_DB_PATH=/data/state.db
VOLUME ["/data"]

# DRY_RUN defaults on; flip via env/secret once you've verified the pipeline.
CMD ["congress-bot", "serve"]
