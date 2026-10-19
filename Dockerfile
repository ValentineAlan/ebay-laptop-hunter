FROM python:3.12-slim

WORKDIR /app
COPY app.py pr_operations.py seo_pages.py /app/

CMD ["python", "-u", "/app/app.py"]
