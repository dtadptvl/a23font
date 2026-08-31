FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 A23FONT_DATA_ROOT=/data A23FONT_HTTP_HOST=0.0.0.0 A23FONT_HTTP_PORT=8090
RUN apt-get update && apt-get install -y --no-install-recommends curl tini && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY pipeline ./pipeline
COPY worker ./worker
COPY templates ./templates
COPY static ./static
VOLUME /data
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD curl -fsS http://127.0.0.1:8090/health/live || exit 1
ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["python","-m","app.web.run"]
