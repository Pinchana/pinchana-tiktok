FROM python:3.13-slim

WORKDIR /workspace/pinchana-tiktok

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy pinchana-core (local path dependency) first
COPY pinchana-core/pyproject.toml pinchana-core/uv.lock ../pinchana-core/
RUN mkdir -p ../pinchana-core/src
COPY pinchana-core/src ../pinchana-core/src

# Copy scraper package files
COPY pinchana-tiktok/pyproject.toml pinchana-tiktok/uv.lock ./
RUN uv sync --frozen --no-install-project

COPY pinchana-tiktok/src ./src

RUN mkdir -p /app/cache
ENV CACHE_PATH=/app/cache
ENV CACHE_MAX_SIZE_GB=10.0

EXPOSE 8080
CMD ["uv", "run", "uvicorn", "pinchana_tiktok.main:app", "--host", "0.0.0.0", "--port", "8080"]
