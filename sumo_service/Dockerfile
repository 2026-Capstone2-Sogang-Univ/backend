FROM ghcr.io/eclipse-sumo/sumo:main

# Install Python 3.11 from deadsnakes PPA on Ubuntu 22.04 Jammy.
# The official SUMO image ships Python 3.10; we need 3.11 to match pyproject.toml.
RUN apt-get update && apt-get install -y --no-install-recommends \
    software-properties-common \
    && add-apt-repository ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y --no-install-recommends \
    python3.11 \
    python3.11-dev \
    python3.11-venv \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN python3.11 -m pip install --no-cache-dir uv

WORKDIR /app

# Install dependencies first (layer cache) — project source not needed yet.
COPY pyproject.toml uv.lock ./
RUN python3.11 -m uv sync --frozen --no-dev --no-install-project

# Copy source, then install the project itself.
COPY app/ ./app/
COPY sumo_configs/ ./sumo_configs/
RUN python3.11 -m uv sync --frozen --no-dev

EXPOSE 8080

CMD ["python3.11", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
