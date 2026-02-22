FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
ENV PYTHONPATH=/app

# System deps: build-essential for pip; libexpat1 for Fiona; git+libsqlite3 for tippecanoe
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libexpat1 \
    git \
    libsqlite3-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Tippecanoe for static PMTiles (tile worker)
RUN git clone https://github.com/felt/tippecanoe.git /tmp/tippecanoe \
    && cd /tmp/tippecanoe && make -j$(nproc) && make install \
    && rm -rf /tmp/tippecanoe

# Install Python dependencies
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy application code
COPY app ./app
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic

EXPOSE 8000

# Default command (can be overridden by docker-compose)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

