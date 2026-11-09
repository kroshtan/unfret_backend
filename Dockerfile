FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies (cached layer, only rebuilt when the lock file changes)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Application
COPY README.md LICENSE ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev

# The image index's embedding model, pinned by SHA-256 (veridex.image_index), downloaded at build time so the service
# never fetches code. The catalogue seed is never in the image: an operator imports it (docs/image-index.md).
ENV VERIDEX_IMAGE_MODEL_PATH=/app/models/dinov2-small-int8.onnx
RUN python -m veridex.image_index "$VERIDEX_IMAGE_MODEL_PATH"

RUN useradd --create-home --uid 1000 veridex
USER veridex

EXPOSE 8080
# Behind Caddy and kamal-proxy: trust their forwarded headers; no access log, so visitor addresses stay out of logs.
CMD ["uvicorn", "veridex.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips=*", "--no-server-header", "--no-access-log"]
