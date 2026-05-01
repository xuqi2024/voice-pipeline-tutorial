FROM python:3.10-slim

# 安装 ALSA 工具库（sounddevice 依赖）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libasound2-dev \
    alsa-utils \
    libportaudio2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    websockets==12.0 \
    sounddevice==0.4.6 \
    numpy==1.26.4

COPY mic_capture.py .

# 健康检查：确认 sounddevice 模块可用
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import sounddevice; import websockets" || exit 1

CMD ["python", "mic_capture.py"]
