# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY config.yaml ./

RUN mkdir -p /app/logs

# DNS needs UDP and TCP on port 53 (root inside the container).
EXPOSE 53/udp
EXPOSE 53/tcp

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "main.py", "--health", "--config", "/app/config.yaml"]

ENTRYPOINT ["python", "main.py"]
CMD ["--config", "/app/config.yaml"]
