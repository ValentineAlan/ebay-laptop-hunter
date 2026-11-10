FROM python:3.12-slim

WORKDIR /app
COPY app.py pr_operations.py seo_pages.py /app/

CMD ["python", "-u", "/app/app.py"]

LABEL org.opencontainers.image.source="https://github.com/ValentineAlan/ebay-laptop-hunter"
COPY app-release.json /usr/local/share/app-release.json
COPY CHANGELOG.md /usr/local/share/app-changelog.md
