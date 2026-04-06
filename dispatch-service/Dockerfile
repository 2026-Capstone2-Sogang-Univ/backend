FROM python:3.11-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files first for layer caching
COPY pyproject.toml uv.lock ./

# Install dependencies (no dev deps in production image)
RUN uv sync --frozen --no-dev

# Copy application source
COPY app/ ./app/

CMD ["uv", "run", "python", "-m", "app"]
