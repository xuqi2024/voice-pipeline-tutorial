FROM python:3.10-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi==0.110.2 \
    "uvicorn[standard]==0.29.0" \
    websockets==12.0 \
    httpx==0.27.0 \
    python-multipart==0.0.9

COPY app.py .
COPY static/ ./static/

EXPOSE 8080

CMD ["python", "app.py"]
