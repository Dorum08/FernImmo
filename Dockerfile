FROM python:3.12-slim

# Mit --build-arg PLAYWRIGHT=1 wird zusätzlich ein Headless-Chromium installiert
# (größeres Image, hilft bei Portalen mit Bot-Schutz).
ARG PLAYWRIGHT=0

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TZ=Europe/Berlin
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt \
 && if [ "$PLAYWRIGHT" = "1" ]; then pip install playwright && playwright install --with-deps chromium; fi

COPY fernimmo ./fernimmo

RUN useradd -m fernimmo && mkdir -p /app/data && chown fernimmo /app/data
USER fernimmo

EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/health')"
CMD ["python", "-m", "fernimmo", "-c", "/app/config.yaml", "serve"]
