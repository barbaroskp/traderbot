FROM python:3.11-slim

LABEL maintainer="bingx-agent"
LABEL description="BingX Futures Trading Bot"

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY pyproject.toml .
RUN pip install --no-cache-dir . && pip install --no-cache-dir ".[dev]"

# Copy source
COPY src/ src/
COPY tests/ tests/

# Create data directory
RUN mkdir -p /app/data

# Non-root user
RUN useradd -m -r agent && chown -R agent:agent /app
USER agent

# Volume for persistent data (SQLite + logs)
VOLUME ["/app/data"]

# Default: run paper mode
ENTRYPOINT ["python", "-m", "src.cli"]
CMD ["run-paper"]

# Healthcheck: validates config can load
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD python -m src.cli validate-config || exit 1
