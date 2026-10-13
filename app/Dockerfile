FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

RUN groupadd --gid 10001 flexpay \
    && useradd --uid 10001 --gid flexpay --no-create-home --shell /usr/sbin/nologin flexpay

WORKDIR /app
COPY --chown=10001:10001 app/ /app/
USER 10001:10001

EXPOSE 8080
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=2 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2).read()"]

ENTRYPOINT ["python", "-u", "main.py"]
