FROM python:3.10-slim
WORKDIR /app
RUN pip install --no-cache-dir \
    fastapi==0.110.2 \
    "uvicorn[standard]==0.29.0" \
    httpx==0.27.0
COPY app.py .
EXPOSE 8006
CMD ["python", "app.py"]
