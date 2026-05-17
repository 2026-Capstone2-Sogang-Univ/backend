FROM ghcr.io/eclipse-sumo/sumo:main

# python3.11 is available in Ubuntu 22.04 universe repo — no PPA needed.
RUN apt-get update && apt-get install -y --no-install-recommends \
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

# Use the Python package's SUMO binary (ships with all required shared libs).
ENV SUMO_HOME=/app/.venv/lib/python3.11/site-packages/sumo
ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8080

CMD ["/app/.venv/bin/python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--ws-per-message-deflate"]
