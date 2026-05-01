# 实时语音处理管道教程

> 基于 Silero-VAD + FunASR + voiceprint-api 的完整语音处理管道

## 🏗️ 架构总览

```
麦克风 (USB: 0c45:6369)
    │
    │ WebSocket (PCM 16k 16bit mono)
    ▼
┌─────────────────────────────────────┐
│        VAD Service (Docker)          │
│     Silero-VAD 语音活动检测          │
│  - 接收音频流                        │
│  - 检测人声片段                      │
│  - 分发语音数据                      │
└───────────────┬─────────────────────┘
                │ 检测到人声
        ┌───────┴────────┐
        ▼                ▼
┌──────────────┐  ┌──────────────────┐
│ FunASR 服务  │  │ voiceprint-api   │
│ (Docker)     │  │ (Docker)         │
│ 中文实时 ASR │  │ 声纹识别         │
│ WebSocket    │  │ 基于 3D-Speaker  │
│ 端口: 10095  │  │ REST API         │
└──────┬───────┘  │ 端口: 8005       │
       │          └────────┬─────────┘
       │ 识别文本           │ 说话人ID
       └────────┬──────────┘
                ▼
        ┌───────────────┐
        │  下游处理      │
        │  (LLM 等)     │
        └───────────────┘
```

## 📋 服务清单

| 服务 | 端口 | 协议 | 说明 |
|------|------|------|------|
| mic-service | — | — | 麦克风采集，推流到 VAD |
| VAD Service | 8765 | WebSocket | 语音活动检测，中转枢纽 |
| FunASR | 10095 | WebSocket | 中文实时语音识别 |
| voiceprint-api | 8005 | HTTP REST | 声纹注册与识别 |
| MySQL (voiceprint) | 3306 | TCP | 声纹向量存储 |

## 🚀 快速开始

### 前置条件

```bash
# 确认麦克风设备存在
lsusb | grep "0c45:6369"

# 检查 Docker 和 docker-compose
docker --version
docker compose version
```

### 一键启动所有服务

```bash
cd voice-pipeline-tutorial

# 1. 启动 FunASR（首次需下载模型，约 2-5 分钟）
cd services/funasr-service && docker compose up -d && cd ../..

# 2. 启动 voiceprint（含 MySQL，首次下载模型约 30-60 秒）
cd services/voiceprint-service && docker compose up -d && cd ../..

# 3. 启动 VAD 服务
cd services/vad-service && docker compose up -d && cd ../..

# 4. 启动麦克风服务（最后启动，依赖 VAD）
cd services/mic-service && docker compose up -d && cd ../..

# 5. 查看所有服务状态
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

# 6. 一键验证所有服务
bash tests/run_all_tests.sh
```

### 启动麦克风服务（Docker）

麦克风已作为 Docker 服务运行，会自动识别 UGREEN Camera 并推流到 VAD：

```bash
# 查看麦克风服务日志（实时音量）
docker logs -f mic-service

# 示例输出（静音时）：
# [MIC] 已发现设备: UGREEN Camera (index=5, channels=2)
# [MIC] 音量: ░░░░░░░░░░ (静音)
# 对着麦克风说话时会看到 ████ 音量条
```

## 📚 详细文档

1. [架构概述与数据流](docs/01-overview.md)
2. [麦克风采集与 WebSocket](docs/02-audio-capture.md)
3. [VAD 服务部署](docs/03-vad-service.md)
4. [FunASR 服务部署](docs/04-funasr-service.md)
5. [voiceprint-api 部署](docs/05-voiceprint-service.md)
6. [3D-Speaker 原理解析](docs/06-3d-speaker-explained.md)
7. [测试与验证指南](docs/07-testing-guide.md)
8. [**日志查看与调试**](docs/08-logging-guide.md) ← 出问题先看这里

## 🔧 单点测试

每个服务都可以独立测试：

```bash
# 测试 FunASR
python tests/test_funasr.py

# 测试 voiceprint-api
python tests/test_voiceprint.py

# 测试 VAD
python tests/test_vad.py

# 全部测试
bash tests/run_all_tests.sh
```

## 🎙️ 硬件信息

- **麦克风**: Bus 005 Device 002: ID 0c45:6369 Microdia UGREEN Camera（内置麦克风）
- **音频格式**: PCM 16kHz, 16-bit, Stereo（容器内录制后转为 Mono 发给 VAD）
- **音频库**: sounddevice（替代 PyAudio，无需编译，Debian 兼容性更好）

## 🐛 遇到问题？

详见 [日志查看与调试指南](docs/08-logging-guide.md)，包含：
- 各服务日志查看命令
- 日志含义解读
- 全链路排查流程
