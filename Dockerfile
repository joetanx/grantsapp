# syntax=docker/dockerfile:1

# --- Stage 1: build ---
FROM python:slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# Install build dependencies for compiling Python packages
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*

# Create a clean virtual environment
RUN python -m venv /opt/venv
# Update PATH so subsequent pip commands automatically use the virtual environment
ENV PATH="/opt/venv/bin:$PATH"

# Install Python packages inside the virtual environment
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# --- Stage 2: runtime ---
FROM python:slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Permanently append the copied virtual environment to the runtime PATH
    PATH="/opt/venv/bin:$PATH"

# Create a non-root user with fixed UID/GID 1000:1000 (matches NFS share permissions)
RUN groupadd --gid 1000 app && useradd --uid 1000 --gid 1000 --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

# 1. Copy the virtual environment from the builder stage
# 2. Automatically change ownership to the non-root user
COPY --from=builder --chown=1000:1000 /opt/venv /opt/venv

# Copy application code (keep commented if mounting from volume)
# COPY --chown=1000:1000 . .

# Switch away from root to the non-root application user
USER 1000:1000

EXPOSE 8080

# App aiohttp binds 0.0.0.0:8080
CMD ["python", "app.py"]
