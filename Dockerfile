FROM python:3.12-slim

ARG VCS_REF=unknown
LABEL org.opencontainers.image.version="0.10.66" \
      org.opencontainers.image.revision=$VCS_REF

WORKDIR /app
COPY app.py pr_operations.py seo_pages.py /app/

CMD ["python", "-u", "/app/app.py"]

LABEL org.opencontainers.image.source="https://github.com/ValentineAlan/ebay-laptop-hunter"
COPY app-release.json /usr/local/share/app-release.json
COPY CHANGELOG.md /usr/local/share/app-changelog.md
