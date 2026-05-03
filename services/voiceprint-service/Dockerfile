FROM python:3.10-slim

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    libsndfile1 \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 克隆 voiceprint-api 源码
RUN git clone --depth=1 https://github.com/xinnan-tech/voiceprint-api.git /app

# 先安装 torch CPU-only（比 PyPI 的 CUDA 版本小约 5 倍）
RUN pip install --no-cache-dir \
    torch==2.2.2 torchaudio==2.2.2 \
    --index-url https://download.pytorch.org/whl/cpu

# 安装其余依赖（排除已安装的 torch/torchaudio）
RUN pip install --no-cache-dir \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    modelscope==1.13.0 \
    datasets==2.18.0 \
    pyarrow==14.0.2 \
    numpy==1.23.5 \
    packaging==21.3 \
    addict==2.4.0 \
    transformers==4.52.4 \
    sentencepiece==0.2.0 \
    soundfile==0.13.1 \
    pyyaml==6.0.1 \
    fastapi==0.110.2 \
    uvicorn==0.29.0 \
    PyMySQL==1.1.0 \
    python-multipart==0.0.9 \
    librosa==0.10.1 \
    loguru==0.7.2

# 复制配置文件
COPY voiceprint.yaml /app/data/.voiceprint.yaml

# 应用补丁：允许 speaker_ids 为空、多样本累积平均、说话人列表接口
COPY patches/api/v1/voiceprint.py /app/app/api/v1/voiceprint.py
COPY patches/services/voiceprint_service.py /app/app/services/voiceprint_service.py
COPY patches/database/voiceprint_db.py /app/app/database/voiceprint_db.py

EXPOSE 8005

CMD ["python", "-m", "app.main"]
