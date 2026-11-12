# ГРАДДОК — image for pi5 (arm64), built on the Pi itself
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    CONFIG_FILE=/config/config.yaml \
    DATA_DIR=/data \
    BROWSE_ROOTS=/sources

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# committees.yaml is copied next to config.yaml on first start (see entrypoint
# below); the copy in /config is the one edited from the admin panel.
COPY app ./app
COPY static ./static
COPY run.py committees.yaml config.example.yaml ./

VOLUME ["/data", "/config"]
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200 else 1)"

CMD ["sh", "-c", "[ -f /config/committees.yaml ] || cp committees.yaml /config/; exec python run.py"]
