FROM python:3.10-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# 先安装 torch CPU-only（比 PyPI 的 CUDA 版本小约 5 倍）
RUN pip install --no-cache-dir \
    torch==2.2.2 torchaudio==2.2.2 \
    --index-url https://download.pytorch.org/whl/cpu

# 安装其余依赖
RUN pip install --no-cache-dir \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    silero-vad==5.1.2 \
    websockets==12.0 \
    numpy==1.26.4 \
    httpx==0.27.0 \
    aiofiles==23.2.1

COPY app.py .

EXPOSE 8765

CMD ["python", "app.py"]
