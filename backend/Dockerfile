FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies (needed for psycopg2 and other packages)
RUN apt-get update && apt-get install -y \
    gcc \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Copy only the files needed to resolve the package metadata and install deps.
# pyproject.toml uses setuptools dynamic version (`attr = "app._version.__version__"`),
# so setuptools must be able to locate the `app` package and read `_version.py`
# at install time. We copy the three minimal files that make that possible:
#   - pyproject.toml         — declares the dynamic version attr
#   - app/__init__.py        — marks `app` as a regular package (required for find)
#   - app/_version.py        — the literal that the attr resolves to
# Code edits to the rest of the app DO NOT invalidate this layer. A version
# bump touches _version.py, which intentionally rebuilds the dep layer —
# that's the cost of changing what gets installed, and it's bounded to once
# per release rather than once per commit.
COPY pyproject.toml /app/
COPY app/__init__.py /app/app/__init__.py
COPY app/_version.py /app/app/_version.py

# Install dependencies
RUN pip install --no-cache-dir -e .

# Copy the rest of the application
COPY . /app/

# Copy executor script for user containers
COPY container_executor.py /app/executor.py

# Create directory for logs
RUN mkdir -p /app/logs

# Expose the port the app runs on
EXPOSE 8000

# Run database migrations on startup, then start the application with multiple workers
# UVICORN_WORKERS environment variable can override default of 4
CMD alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS:-4}