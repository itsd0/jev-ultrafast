# Cloud-browser server image. No Chrome inside: tasks must use browser "cloud".
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    JEV_BROWSER=cloud \
    JEV_SERVER_HOST=0.0.0.0 \
    JEV_SERVER_PORT=8770

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir .

EXPOSE 8770
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request,json; assert json.load(urllib.request.urlopen('http://127.0.0.1:8770/api/health', timeout=4)) == {'status': 'ok'}"
CMD ["jev-server"]
