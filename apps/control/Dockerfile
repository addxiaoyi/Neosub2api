FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    UNIFIED_DATA_DIR=/data \
    UNIFIED_HOST=0.0.0.0 \
    UNIFIED_PORT=8000

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000
CMD ["python", "-m", "app.main"]
