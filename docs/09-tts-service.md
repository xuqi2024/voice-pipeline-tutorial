# TTS 方案选型分析

> 当前机器环境：CPU-only（无 NVIDIA GPU / CUDA），Ubuntu 22.04，124 GB 磁盘，Docker 29.2，Python 3.10

---

## 分析结论速览

| 方案 | 本机可用 | 质量 | 原因 |
|------|---------|------|------|
| **ChatTTS** | ⚠️ 勉强 | 中等 | 官方推荐 4GB VRAM；CPU 推理 RTF ≈ 10+，30 秒音频需数分钟 |
| **IndexTTS** | ❌ 不可用 | 高 | 明确要求 CUDA 12.8+；无 GPU 完全无法正常运行 |
| **PaddleSpeech** | ✅ 可用 | 中等 | 官方支持 CPU 推理；中文 TTS 质量一般 |
| **FishSpeech** | ❌ 不可用 | 高 | 推理需大量 VRAM（≥8GB），CPU 极慢 |
| **GPT-SoVITS V2** | ⚠️ 勉强 | 高 | 可 CPU 运行但极慢；适合离线少量合成 |
| **GPT-SoVITS V3** | ❌ 不可用 | 极高 | 比 V2 更吃 GPU，CPU 不可行 |
| **MiniMax TTS API** | ✅ **最佳** | 极高 | 云端 API，无需本地 GPU；中英文最自然 |

**选定方案：MiniMax TTS API（`tts-service`）**

---

## 详细分析

### 1. ChatTTS
- **项目**：https://github.com/2noise/ChatTTS
- **模型大小**：约 2GB（HuggingFace 4 万小时预训练版本）
- **GPU 需求**：FAQ 明确写"至少 4GB GPU 显存"，4090 上 RTF ≈ 0.3
- **CPU 可行性**：理论可运行（PyTorch CPU 模式），但 RTF 估算 **>10**，即 30 秒音频合成耗时 >5 分钟，不可用于实时/在线场景
- **结论**：❌ 当前机器不可用于生产

### 2. IndexTTS
- **项目**：https://github.com/index-tts/index-tts
- **GPU 需求**：文档明确要求 **CUDA 12.8+**；配有 GPU 检测脚本 `tools/gpu_check.py`
- **CPU 可行性**：零样本 TTS + 自回归架构，CPU 推理几乎不可行
- **结论**：❌ 无 GPU 完全不可部署

### 3. PaddleSpeech（本地 CPU 可用）
- **项目**：https://github.com/PaddlePaddle/PaddleSpeech
- **CPU 支持**：官方 CPU 版 PaddlePaddle（`paddlepaddle` 而非 `paddlepaddle-gpu`）
- **中文模型**：`fastspeech2_csmsc`（参数量小，CPU RTF ≈ 1~3）
- **优缺点**：
  - ✅ 完全本地，无网络依赖
  - ✅ 官方维护，文档完善
  - ⚠️ 音色单一，缺乏情感控制
  - ⚠️ 英文支持有限
- **Docker 镜像大小**：约 3~5 GB（含 PaddlePaddle CPU 依赖）
- **结论**：✅ 可部署，适合对延迟要求不高的离线场景

### 4. FishSpeech
- **项目**：https://github.com/fishaudio/fish-speech
- **GPU 需求**：模型 ≥ 500M 参数，推理需 ≥4GB VRAM
- **CPU 可行性**：极慢，不适合生产
- **结论**：❌ 当前机器不可用

### 5. GPT-SoVITS V2
- **项目**：https://github.com/RVC-Boss/GPT-SoVITS
- **GPU 需求**：推荐 6GB+ VRAM；支持 CPU 推理（添加 `--device cpu`）
- **CPU 可行性**：⚠️ 可运行，但中文 10 秒音频 CPU 耗时约 1~3 分钟
- **特色**：支持声音克隆（5 秒参考音频即可）
- **结论**：⚠️ 少量离线批处理可用，实时使用不可行

### 6. GPT-SoVITS V3
- **GPU 需求**：≥8GB VRAM（比 V2 更重）
- **结论**：❌ CPU 完全不可行

### 7. MiniMax TTS API（选定方案）
- **接口**：`https://api.minimaxi.com/v1/t2a_pro`（国内）/ `https://api.minimax.io/v1/t2a_v2`（国际）
- **模型**：`speech-01`（标准）/ `speech-02`（高质量）/ `speech-01-turbo`（需升级计划）
- **音色**：300+ 内置音色，支持中英文
- **无需本地 GPU**：纯 API 调用
- **延迟**：通常 1~3 秒（含网络）
- **计费**：按字符计费，需账户有余额
- **结论**：✅ **最优选择**，部署简单，质量最高

---

## 已部署服务：`tts-service`

### 服务信息
- **端口**：`8766`
- **容器**：`tts-service`
- **Docker 网络**：`voice-pipeline`

### 启动方式

```bash
# 1. 复制并填写环境变量
cp services/tts-service/.env.example services/tts-service/.env
# 编辑 .env，填入 MINIMAX_API_KEY

# 2. 启动
cd services/tts-service && docker compose up -d --build

# 3. 检查状态
curl http://localhost:8766/health
```

### API 接口

#### `POST /tts` — 原生接口
```bash
curl -X POST http://localhost:8766/tts \
  -H "Content-Type: application/json" \
  -d '{
    "text": "你好，欢迎使用语音合成服务。",
    "voice_id": "female-shaonv",
    "speed": 1.0,
    "format": "mp3"
  }' -o output.mp3
```

#### `POST /v1/audio/speech` — OpenAI 兼容接口
```bash
curl -X POST http://localhost:8766/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{
    "model": "tts-1",
    "input": "Hello, this is a test.",
    "voice": "male-en-Boston",
    "response_format": "mp3"
  }' -o output.mp3
```

#### `GET /voices` — 列出可用音色
```bash
curl http://localhost:8766/voices
```

### 环境变量说明

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MINIMAX_API_KEY` | （必填）| MiniMax API Key |
| `MINIMAX_BASE_URL` | `https://api.minimaxi.com` | 国内用此；国际版用 `https://api.minimax.io` |
| `MINIMAX_MODEL` | `speech-01` | 与 endpoint 对应 |
| `MINIMAX_ENDPOINT` | `t2a_pro` | `t2a_pro`（按次）或 `t2a_v2`（流式，需升级计划）|
| `DEFAULT_VOICE` | `female-shaonv` | 默认音色 |

### MiniMax 账户充值
当前 API Key 已通过认证，账户余额不足需在 [platform.minimaxi.com](https://platform.minimaxi.com) 充值后即可正常使用。

---

## 替代方案：PaddleSpeech（完全离线）

如需完全离线 CPU TTS，可参考以下 Docker 部署（中文质量一般，延迟高）：

```dockerfile
FROM python:3.9-slim
RUN apt-get update && apt-get install -y ffmpeg libsndfile1 && rm -rf /var/lib/apt/lists/*
RUN pip install paddlepaddle -f https://paddlepaddle.org.cn/whl/mkl/avx/stable.html
RUN pip install paddlespeech fastapi uvicorn
```

适用场景：内网隔离环境、无互联网访问、对延迟不敏感的批处理任务。
