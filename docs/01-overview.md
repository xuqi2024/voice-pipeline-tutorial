# 架构概述与数据流

## 设计思路

本项目参照 [xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) 的数据流设计，构建一个**本地化的实时语音处理管道**，核心理念是：

> **先检测人声，再处理语音** — VAD 作为门卫，避免无效计算

## 数据流详解

### 完整流程图

```
┌──────────────────────────────────────────────────────────────────┐
│ 用户端 (本机)                                                      │
│                                                                   │
│  麦克风 → PyAudio 采集 → WebSocket Client                         │
│  (0c45:6369)   16kHz/16bit/mono                                  │
└───────────────────────────────┬──────────────────────────────────┘
                                │ ws://localhost:8765
                                │ Binary: PCM 音频块 (512 samples/chunk)
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ VAD Service (Docker, 端口 8765)                                   │
│                                                                   │
│  WebSocket Server                                                 │
│       │                                                           │
│       ▼                                                           │
│  Silero VAD 模型                                                  │
│  - 每 512 样本 (32ms) 推理一次                                    │
│  - 输出概率值 0.0~1.0                                             │
│  - threshold=0.5 判断是否有人声                                   │
│       │                                                           │
│  状态机:                                                          │
│  SILENCE ──(prob>0.5)──→ SPEECH_START                            │
│  SPEECH  ──(prob<0.5, 持续>300ms)──→ SPEECH_END                 │
│       │                                                           │
│  SPEECH_END 时: 将积累的语音片段发出                              │
└──────────────────────┬───────────────────────────────────────────┘
                       │ 检测到完整语音片段 (WAV 字节)
              ┌────────┴────────┐
              │                 │
              ▼                 ▼
┌─────────────────┐   ┌───────────────────────┐
│ FunASR (10095)  │   │ voiceprint-api (8005) │
│                 │   │                       │
│ WebSocket 协议  │   │ POST /voiceprint/      │
│ 发送: PCM 音频  │   │       identify        │
│ 接收: JSON      │   │                       │
│ {               │   │ 请求: multipart/form  │
│  "text": "你好" │   │  - audio: WAV 文件    │
│  "is_final": 1  │   │                       │
│ }               │   │ 响应: JSON            │
└────────┬────────┘   │ {                     │
         │            │  "speaker_id": "张三" │
         │            │  "score": 0.92        │
         │            │ }                     │
         │            └──────────┬────────────┘
         │                       │
         └───────────┬───────────┘
                     ▼
           ┌──────────────────┐
           │  VAD Service     │
           │  聚合结果:        │
           │  {               │
           │   "text": "你好" │
           │   "speaker": {   │
           │    "id": "张三"  │
           │    "score": 0.92 │
           │   }              │
           │  }               │
           └──────────────────┘
```

## 关键设计决策

### 1. 为什么用 VAD 作为中枢？

- **节省计算**: FunASR 和 voiceprint 模型较重，VAD 是轻量级模型，先过滤静音
- **并行处理**: VAD 触发后，ASR 和声纹识别**同时**处理同一段音频
- **解耦**: 客户端只需连接 VAD，无需关心下游服务细节

### 2. 音频格式统一

全链路使用统一格式，避免转换开销：
- **采样率**: 16000 Hz (Silero VAD 要求，FunASR 要求，voiceprint 要求)
- **位深**: 16-bit PCM (signed int16)
- **声道**: 1 (Mono)
- **块大小**: 512 samples = 32ms (Silero VAD 推荐)

### 3. VAD 状态机

```
         prob > 0.5               prob < 0.5 持续 > 300ms
SILENCE ──────────────→ SPEAKING ──────────────────────→ END
                              ↑                              │
                              └──────────────────────────────┘
                              (重置缓冲区，发送音频到 ASR+声纹)
```

## 与 xiaozhi-esp32-server 的对比

| 方面 | xiaozhi 项目 | 本项目 |
|------|-------------|--------|
| 音频来源 | ESP32 硬件设备 | USB 麦克风 (PC) |
| 传输协议 | WebSocket over Internet | 本地 WebSocket |
| VAD | 集成在服务端 | 独立 Docker 服务 |
| ASR | FunASR / 讯飞 API | FunASR (本地) |
| 声纹 | voiceprint-api | voiceprint-api (相同) |
| 数据流 | 设备→服务器→LLM | 麦克风→VAD→ASR+声纹 |
