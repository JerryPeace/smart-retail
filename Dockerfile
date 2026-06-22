# syntax=docker/dockerfile:1.7

# ===================================================================
# Stage 1: Builder — install dependencies
# ===================================================================
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Copy lock files first to take advantage of the Docker layer cache
COPY pyproject.toml uv.lock* .python-version ./

# Install dependencies (without installing the project itself, to speed up caching)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev || \
    uv sync --no-install-project --no-dev

# Copy the project code
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./

# Install the project itself
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev

# ===================================================================
# Stage 2: Runtime — carry only the venv + code
# ===================================================================
FROM python:3.14-slim-bookworm AS runtime

WORKDIR /app

# Copy the virtual environment and code from the builder
COPY --from=builder /app /app

# Add the venv to PATH
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

CMD ["uvicorn", "cleaner.main:app", "--host", "0.0.0.0", "--port", "8000"]
