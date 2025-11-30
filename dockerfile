FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    POETRY_VERSION=1.8.3 \
    POETRY_VIRTUALENVS_CREATE=false \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
  && rm -rf /var/lib/apt/lists/*

RUN pip install "poetry==${POETRY_VERSION}"

COPY pyproject.toml poetry.lock* /app/
RUN poetry install --only main --no-interaction --no-ansi

COPY src /app/src

RUN useradd --create-home --uid 10001 appuser \
  && mkdir -p /data/artifacts \
  && chown -R appuser:appuser /data /app

USER appuser

EXPOSE 8000