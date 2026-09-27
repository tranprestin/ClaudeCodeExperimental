# Full-featured image (static + JavaScript rendering) for Render, Railway, Fly.io, Cloud Run...
# The Playwright base image ships Chromium and its system libraries.
FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt playwright==1.55.0 gunicorn==23.0.0
COPY app.py scraper.py ./
COPY templates ./templates

ENV HOST=0.0.0.0 PORT=8000
EXPOSE 8000
# One worker, several threads: scrapes are I/O bound and each browser is heavy.
CMD gunicorn app:app --bind 0.0.0.0:${PORT} --workers 1 --threads 4 --timeout 120
