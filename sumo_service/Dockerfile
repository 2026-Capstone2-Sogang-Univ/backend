FROM python:3.11-slim

# eclipse-sumo pip package bundles the SUMO binary for Linux x86_64,
# so no separate apt installation is required.
RUN pip install --no-cache-dir uv

WORKDIR /app

# Install dependencies first (layer cache) — project source not needed yet.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy source, then install the project itself.
COPY app/ ./app/
COPY sumo_configs/ ./sumo_configs/
RUN uv sync --frozen --no-dev

EXPOSE 8080

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
