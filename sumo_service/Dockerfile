FROM python:3.11-slim

# eclipse-sumo pip package bundles the SUMO binary for Linux x86_64,
# so no separate apt installation is required.
RUN pip install --no-cache-dir uv

WORKDIR /app

# Install Python dependencies first (layer cache)
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen

# Copy source
COPY app/ ./app/
COPY sumo_configs/ ./sumo_configs/

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
