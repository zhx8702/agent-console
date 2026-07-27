FROM ghcr.io/astral-sh/uv:0.9.28@sha256:59240a65d6b57e6c507429b45f01b8f2c7c0bbeee0fb697c41a39c6a8e3a4cfb AS uv

FROM python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=uv /uv /uvx /bin/

RUN useradd --create-home --shell /bin/bash appuser
RUN mkdir -p /data/config /data/draw /data/amap \
    && touch /data/config/.env \
    && chown -R appuser:appuser /data/config /data/draw /data/amap \
    && chmod 700 /data/config /data/draw /data/amap \
    && chmod 600 /data/config/.env

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY app ./app
COPY plugins ./plugins
COPY migrations ./migrations
COPY config ./config

RUN uv sync --frozen --no-dev --no-editable
RUN touch /app/.env && chown appuser:appuser /app/.env && chmod 600 /app/.env

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
