FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies (cached layer, only rebuilt when the lock file changes)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Chromium + system libraries for the reverse image search skill
RUN playwright install chromium --with-deps && rm -rf /var/lib/apt/lists/*

# Application
COPY README.md LICENSE ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 1000 veridex
USER veridex

EXPOSE 8080
CMD ["uvicorn", "veridex.main:app", "--host", "0.0.0.0", "--port", "8080"]
