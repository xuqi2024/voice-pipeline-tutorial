# VAD 服务部署

## 概述

VAD（Voice Activity Detection）服务是整个管道的**入口和中枢**。

- 接收麦克风的实时音频流
- 使用 [Silero VAD](https://github.com/snakers4/silero-vad) 检测人声
- 检测到完整语音后，**同时转发**给 FunASR 和 voiceprint-api

## 部署步骤

### 1. 构建并启动

```bash
cd services/vad-service
docker compose up -d --build

# 查看日志
docker compose logs -f
```

### 2. 验证服务

```bash
# 检查容器状态
docker compose ps
# 预期: vad-service  running

# 检查端口
ss -tlnp | grep 8765

# 运行验证测试
python ../../tests/test_vad.py
```

## 工作原理

### Silero VAD 模型

Silero VAD 是一个轻量级、精准的语音活动检测模型：

- **模型大小**: ~1.8 MB (ONNX 格式)
- **延迟**: < 1ms/chunk (CPU)
- **输入**: 512 个 int16 样本 (32ms @ 16kHz)
- **输出**: 概率值 0.0 ~ 1.0

```python
# 工作方式
prob = model(audio_chunk, sample_rate=16000)
is_speech = prob > 0.5  # threshold
```

### 状态机逻辑

```
初始状态: SILENCE
─────────────────────────────────────────────────────
事件                      → 下一状态    动作
─────────────────────────────────────────────────────
prob > 0.5               → SPEAKING   开始录制缓冲区
SPEAKING + prob < 0.5
  持续 < silence_ms=300  → SPEAKING   继续缓冲 (短暂停顿)
SPEAKING + prob < 0.5
  持续 > silence_ms=300  → SILENCE    发送完整片段给 ASR+声纹
─────────────────────────────────────────────────────
```

### 关键参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `threshold` | 0.5 | 判断人声的概率阈值 |
| `min_speech_ms` | 250 | 最短有效语音时长 |
| `min_silence_ms` | 300 | 语音结束后等待时长 |
| `max_speech_ms` | 10000 | 单次语音最大时长（防止无限录制） |

## 配置说明

可通过环境变量调整（在 `docker-compose.yml` 中设置）：

```yaml
environment:
  - VAD_THRESHOLD=0.5
  - MIN_SILENCE_MS=300
  - FUNASR_WS_URL=ws://funasr:10095
  - VOICEPRINT_API_URL=http://voiceprint-api:8005
```

## 日志说明

```
[VAD] Client connected: 127.0.0.1
[VAD] chunk=512 prob=0.02 → SILENCE
[VAD] chunk=512 prob=0.87 → SPEECH_START ←── 检测到人声
[VAD] chunk=512 prob=0.95 → SPEAKING
[VAD] chunk=512 prob=0.91 → SPEAKING
[VAD] chunk=512 prob=0.03 → SILENCE (30ms)
[VAD] chunk=512 prob=0.01 → SILENCE (60ms)
...
[VAD] chunk=512 prob=0.01 → SILENCE (330ms) → SEND ←── 发送语音
[VAD] Speech segment: 1280ms, 20480 samples
[VAD] → FunASR: sending 40960 bytes
[VAD] → voiceprint: sending WAV 44KB
[VAD] FunASR result: {"text": "你好", "is_final": 1}
[VAD] voiceprint result: {"speaker_id": "user1", "score": 0.93}
[VAD] Final result: {"text": "你好", "speaker": {"id": "user1", "score": 0.93}}
```
