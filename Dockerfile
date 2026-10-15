# Dockerfile for Emby Collection Manager with Web UI
FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=on \
    WEBUI_PORT=8282

# Set workdir
WORKDIR /app

# Install dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ ./src/
COPY main.py ./
COPY web/ ./web/
COPY config/ ./config/
COPY resources/ ./resources/

# Create directories for user-defined lists
RUN mkdir -p /app/traktlists /app/mdblists /app/logs

# Expose web UI port
EXPOSE 8282

# Expose volumes for mounting configs and user lists
VOLUME ["/app/config", "/app/traktlists", "/app/mdblists"]

# Add entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
CMD []
