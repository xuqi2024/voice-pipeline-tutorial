# 服务内部组件详解

本文深入剖析每个服务的内部实现，帮助你理解设计决策、调优参数以及未来扩展方向。

---

## 目录

1. [整体数据流（带时序）](#1-整体数据流带时序)
2. [VAD Service 内部解析](#2-vad-service-内部解析)
3. [FunASR Service 内部解析](#3-funasr-service-内部解析)
4. [Voiceprint Service 内部解析](#4-voiceprint-service-内部解析)
5. [LLM Service 内部解析](#5-llm-service-内部解析)
6. [TTS Service 内部解析](#6-tts-service-内部解析)
7. [Dashboard Service 内部解析](#7-dashboard-service-内部解析)
8. [三重防护机制](#8-三重防护机制)
9. [设备注册与广播系统](#9-设备注册与广播系统)
10. [参数调优速查](#10-参数调优速查)

---

## 1. 整体数据流（带时序）

```
时间线（用户说"你好"为例）
────────────────────────────────────────────────────────────────
t=0ms    麦克风/ESP32 采集 PCM (16kHz, 16-bit, mono)
         └─ WebSocket binary 推送 → VAD-Service:8765

t=0~300ms  VAD 逐块检测 (每块 512 samples = 32ms)
           Silero 模型输出 prob，超过 0.5 开始积累

t=300ms  检测到静音 (300ms)，语音片段完整
         └─ build_wav_bytes() 打包 PCM → WAV

t=300ms  asyncio.gather() 并行触发两条支路:
         ├─ [ASR]  FunASR WebSocket:10095  ──→  ~500ms → "你好"
         └─ [VP]   voiceprint-api:8005     ──→  ~200ms → {id:"徐琪", score:0.72}

t=800ms  两支路均完成，VAD 发回给客户端:
         {"text":"你好", "speaker":{"id":"徐琪","score":0.72}}

t=800ms  三重过滤（回声 / 去重 / 声纹）全通过
         └─ call_llm() → LLM-Service:8006/api/chat

t=800ms  Dashboard 收到实时事件流:
         speech_start → asr_result → voiceprint_result → llm_thinking

t=1500ms MiniMax M2.7 返回回复: "你好呀！"

t=1500ms call_tts() → TTS-Service:8766/tts (Edge-TTS, ~800ms)

t=2300ms TTS WAV 缓存到 llm-service 内存
         ├─ push_dashboard(tts_ready + audio_url)  → 浏览器播放
         └─ forward_to_device("*", {type:"tts_url"})
              └─ VAD-Service 广播 → ESP32 WebSocket → I2S DAC → 喇叭
────────────────────────────────────────────────────────────────
全程端到端延迟约 2~3 秒（本地网络，无公网延迟）
```

---

## 2. VAD Service 内部解析

### 2.1 架构图

```
VAD Service (Docker: vad-service)
┌─────────────────────────────────────────────────────┐
│  WebSocket Server :8765                             │
│  ┌──────────────────────────────────────────────┐  │
│  │  handle_client(websocket)                    │  │
│  │  ┌──────────┐  ┌───────────────────────────┐ │  │
│  │  │ Silero   │  │ VAD 状态机                 │ │  │
│  │  │ VAD 模型 │→ │ IDLE → SPEAKING → IDLE     │ │  │
│  │  │ (per-ws) │  │ ↕积累 speech_buffer        │ │  │
│  │  └──────────┘  └─────────────┬─────────────┘ │  │
│  │                              │ WAV bytes       │  │
│  │                    ┌─────────▼─────────┐      │  │
│  │                    │ asyncio.gather()  │      │  │
│  │                    │  ├─ FunASR WS     │      │  │
│  │                    │  └─ voiceprint    │      │  │
│  │                    └─────────┬─────────┘      │  │
│  │                              │                 │  │
│  │              ┌───────────────▼──────────────┐ │  │
│  │              │ 三重过滤                       │ │  │
│  │              │ ① is_tts_echo()               │ │  │
│  │              │ ② is_duplicate_llm()           │ │  │
│  │              │ ③ speaker.id 非空               │ │  │
│  │              └───────────────┬──────────────┘ │  │
│  └──────────────────────────────│────────────────┘  │
│                                 │                    │
│  HTTP API Server :8767          │                    │
│  ┌──────────────────────────┐   │ call_llm()         │
│  │ POST /api/forward        │   └─→ LLM-Service      │
│  │   _connected_devices{}   │                        │
│  │   广播/前缀/单播          │                        │
│  └──────────────────────────┘                        │
└─────────────────────────────────────────────────────┘
```

### 2.2 Silero VAD 算法原理

Silero VAD 是一个 **LSTM + 1D-CNN** 混合模型，专为实时音频流优化：

| 特性 | 参数 |
|------|------|
| 输入 | 512 samples @ 16kHz = **32ms** 窗口 |
| 输出 | `prob ∈ [0,1]`，越高代表越可能是人声 |
| 模型大小 | ~200KB（可运行在 CPU） |
| 延迟 | <5ms per chunk（CPU 推理） |

每个 WebSocket 连接**独立维护一个模型实例**（`load_silero_vad()` 在 `handle_client` 内调用），确保多路并发不互相干扰。

### 2.3 VAD 状态机

```
IDLE ──(prob >= 0.5)──→ SPEAKING ──(silence >= 300ms)──→ IDLE
                            │                                ↑
                            │ (speech_chunks >= MAX)         │
                            └────────────────────────────────┘
                            （超时强制结束，最大 10s）
```

关键判断条件：
```python
# 语音结束：连续静音块数 >= MIN_SILENCE_CHUNKS
MIN_SILENCE_CHUNKS = 300ms / 32ms = ~9 块

# 有效语音：至少 MIN_SPEECH_CHUNKS 块才送去识别
MIN_SPEECH_CHUNKS  = 250ms / 32ms = ~7 块

# 超时强制结束
MAX_SPEECH_CHUNKS  = 10000ms / 32ms = ~312 块
```

### 2.4 并行 ASR + 声纹

```python
# 关键：asyncio.gather 并发执行，不是串行
asr_task = asyncio.create_task(recognize_with_funasr(wav_bytes))
vp_task  = asyncio.create_task(identify_speaker(wav_bytes))
text, speaker = await asyncio.gather(asr_task, vp_task)
# 总耗时 = max(ASR耗时, 声纹耗时)，而非两者之和
```

### 2.5 FunASR 通信协议

VAD 与 FunASR 使用 **WebSocket 2pass 模式**：

```
VAD → FunASR: {"mode":"2pass", "chunk_size":[5,10,5], "is_speaking":true}
VAD → FunASR: [binary audio chunk 100ms]  × N
VAD → FunASR: {"is_speaking": false}       ← 通知结束
FunASR → VAD: {"text":"...", "is_final":0} × N  (中间结果)
FunASR → VAD: {"text":"...", "is_final":1}       (最终结果)
```

2pass 模式含义：**第一趟**流式输出中间结果（conformer），**第二趟**等全局完成后输出精确最终结果（paraformer-large）。

---

## 3. FunASR Service 内部解析

```
FunASR Docker 容器 :10095
┌──────────────────────────────────────────────────────┐
│  funasr-wss-server（WebSocket + HTTPS）               │
│  ┌──────────────────────────────────────────────┐   │
│  │  模型堆栈（自动下载 ModelScope）               │   │
│  │  ├─ paraformer-zh    (离线精确 ASR)           │   │
│  │  ├─ ct-punc          (标点还原)               │   │
│  │  └─ fsmn-vad         (服务端内部 VAD，可选)   │   │
│  └──────────────────────────────────────────────┘   │
│  模型路径: /workspace/models (挂载 ~/.funasr/models)  │
└──────────────────────────────────────────────────────┘
```

FunASR 使用的 **paraformer-zh** 模型特点：
- 基于 **CIF (Continuous Integrate-and-Fire)** 机制，非自回归解码，速度比 Whisper 快 ~4x
- `itn: true` 参数启用**逆文本归一化**（将"一百二十三"转为"123"）
- `hotwords` 参数支持热词权重（未启用，可扩展）

---

## 4. Voiceprint Service 内部解析

### 4.1 模型：CAM++ (ERes2Net)

```
输入音频 → 梅尔频谱图 (MFCC/FBank) → ERes2Net 编码器 → L2归一化 → 192维嵌入向量
                                        ↑
                          基于 ResNet + SE 注意力机制
                          统计池化 → 说话人嵌入空间映射
```

- **192 维**嵌入向量，代表说话人在高维空间的位置
- 同一说话人的不同录音：余弦相似度 > 0.55（阈值）
- 不同说话人：余弦相似度通常 < 0.35

### 4.2 识别流程

```python
# voiceprint_service.py
def identify_voiceprint(candidates, audio_bytes):
    emb_query = model.extract_embedding(audio_bytes)    # 1. 提取查询嵌入
    all_embs  = voiceprint_db.get_voiceprints(candidates)  # 2. 从 MySQL 加载所有注册嵌入
    
    best_id, best_score = None, 0.0
    for speaker_id, stored_emb in all_embs.items():
        score = cosine_similarity(emb_query, stored_emb)  # 3. 余弦相似度
        if score > best_score:
            best_id, best_score = speaker_id, score
    
    if best_score >= similarity_threshold:  # 4. 阈值过滤
        return best_id, best_score
    return None, best_score
```

### 4.3 多样本累积平均（提高精度的核心）

```python
# voiceprint_db.py - save_voiceprint(accumulate=True)
#
# 问题：单次录音可能受环境噪声、距离、情绪影响，嵌入偏移
# 解法：加权平均多个录音样本，使嵌入更接近"说话人中心"
#
# 数学原理：
#   merged = (N × old_emb + new_emb) / (N + 1)
#   merged = merged / ||merged||   ← L2 归一化回单位球面

old_count = _get_sample_count(speaker_id)
merged = (old_count * old_emb + new_emb) / (old_count + 1)
merged = merged / np.linalg.norm(merged)
```

**建议注册方式**：至少录制 **3~5 个样本**（不同距离、角度），每次勾选"累积"复选框。

### 4.4 MySQL 存储结构

```sql
CREATE TABLE voiceprints (
    speaker_id     VARCHAR(64) PRIMARY KEY,
    feature_vector BLOB NOT NULL,   -- 192维 float32 = 768 bytes
    sample_count   INT DEFAULT 1,    -- 累积样本数（用于加权）
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at     TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
```

---

## 5. LLM Service 内部解析

### 5.1 组件图

```
LLM Service :8006
┌──────────────────────────────────────────────────────┐
│                                                      │
│  POST /api/chat                                      │
│  ┌─────────────────────────────────────────────┐    │
│  │ 1. 构建 messages = history + user_msg       │    │
│  │ 2. MiniMax API (Anthropic 格式)             │    │
│  │    POST https://api.minimaxi.com/...        │    │
│  │    model: MiniMax-M2.7, max_tokens: 300     │    │
│  │ 3. 解析 content[].type=text                 │    │
│  │ 4. history.append(user+assistant)           │    │
│  └─────────────────────────────────────────────┘    │
│                │                                     │
│         ┌──────▼──────┐    ┌───────────────────┐    │
│         │  call_tts() │    │  push_dashboard() │    │
│         │  :8766/tts  │    │  llm_response 事件 │    │
│         └──────┬──────┘    └───────────────────┘    │
│                │ WAV bytes                            │
│  ┌─────────────▼──────────────────┐                 │
│  │ _audio_cache{audio_id: bytes}  │                 │
│  │ 最多 20 条，FIFO 淘汰           │                 │
│  └─────────────┬──────────────────┘                 │
│                │ audio_url                            │
│         ┌──────▼────────────────────────┐            │
│         │  forward_to_device("*", ...)  │            │
│         │  → VAD HTTP :8767/api/forward │            │
│         │  → 广播给所有 WebSocket 设备  │            │
│         └───────────────────────────────┘            │
│                                                      │
│  GET /api/audio/{audio_id}  ← ESP32 HTTP 下载        │
│  DELETE /api/history/{speaker_id}                    │
└──────────────────────────────────────────────────────┘
```

### 5.2 对话历史管理

```python
# 每个说话人独立的 deque，最多保留 20 条消息（10 轮对话）
histories: dict[str, deque] = defaultdict(lambda: deque(maxlen=20))
# key = speaker_id（声纹识别的名字，如"徐琪"）
# value = [{"role":"user","content":...}, {"role":"assistant","content":...}, ...]

# 注意：重启后历史丢失（纯内存）
# 如需持久化 → 可改为 SQLite / Redis
```

### 5.3 MiniMax Anthropic 兼容 API 调用

```python
# MiniMax 提供 Anthropic 兼容端点，格式完全相同
POST https://api.minimaxi.com/anthropic/v1/messages
Headers:
  x-api-key: <MINIMAX_API_KEY>
  anthropic-version: 2023-06-01
Body:
  {
    "model": "MiniMax-M2.7",
    "system": "你是简洁智能的语音助手...",
    "messages": [{"role":"user","content":"你好"}],
    "max_tokens": 300
  }
```

### 5.4 TTS 音频 URL 分发机制

```
LLM 生成 audio_id = uuid4().hex[:12]
http://{HOST_IP}:8006/api/audio/{audio_id}  ← 对局域网可访问

两条播放路径：
① 浏览器：Dashboard SSE → tts_ready 事件 → fetch(audio_url) → Web Audio API
② ESP32：WebSocket 收到 {type:"tts_url", url:...} → HTTP GET audio_url → I2S 播放

WAV 格式细节：
- 采样率：32000 Hz（Edge-TTS 固定输出）
- 位深：16-bit signed
- 声道：单声道
- 时长计算：(wav_size - header) / (32000 × 2) 秒
```

---

## 6. TTS Service 内部解析

```
TTS Service :8766 (Edge-TTS 封装)
┌───────────────────────────────────────────────────────┐
│  POST /tts                                            │
│  {text, voice_id, format, stream}                    │
│  └─ edge-tts (微软云 TTS 免费端点)                    │
│     └─ 返回 MP3/WAV bytes                             │
│                                                       │
│  可用中文音色：                                        │
│  female-shaonv   → zh-CN-XiaoxiaoNeural (少女音)      │
│  presenter_male  → zh-CN-YunxiNeural    (主播男声)    │
│  male-qn-badao   → zh-CN-YunyangNeural  (霸道男声)    │
│  ... 共 11 个                                         │
│                                                       │
│  POST /v1/audio/speech  (OpenAI 兼容接口)             │
│  GET  /voices          (列出所有音色)                  │
│  GET  /health                                         │
└───────────────────────────────────────────────────────┘
```

**注意**：Edge-TTS 使用微软云，需要**网络可访问**。TTS 延迟通常 500ms~2s，主要取决于网络。

---

## 7. Dashboard Service 内部解析

```
Dashboard Service :8080
┌──────────────────────────────────────────────────────┐
│  FastAPI + SSE (Server-Sent Events)                  │
│  ┌─────────────────────────────────────────────┐    │
│  │ POST /api/vad-event   ← 各服务推送事件       │    │
│  │   → asyncio.Queue → SSE 流                  │    │
│  └───────────────────────┬─────────────────────┘    │
│                           │                          │
│  GET  /api/events         │ SSE 流 (text/event-stream)│
│  ┌────────────────────────▼────────────────────┐    │
│  │ 浏览器 EventSource → 实时更新 UI            │    │
│  └─────────────────────────────────────────────┘    │
│                                                      │
│  代理 API（转发到 voiceprint-api）：                  │
│  GET  /api/speakers/           → 列出说话人           │
│  POST /api/speakers/register   → 注册声纹             │
│  DELETE /api/speakers/{id}     → 删除声纹             │
└──────────────────────────────────────────────────────┘

事件类型一览：
  speech_start      → VAD 检测到语音开始
  speech_end        → 语音结束，duration_ms
  asr_result        → ASR 文字结果
  voiceprint_result → 声纹识别结果 + score
  llm_thinking      → LLM 开始处理
  llm_response      → LLM 回复完成
  tts_ready         → TTS 音频就绪 + audio_url
  result            → 综合结果（text + speaker）
  system            → 系统提示/错误
```

---

## 8. 三重防护机制

防止 TTS 音频被麦克风拾取后形成"对话死循环"，以及多麦重复触发。

### 第一重：回声检测 `is_tts_echo()`

```
触发条件（同时满足）：
  ① 当前时间 ∈ [TTS开始时间, TTS结束时间 + 4秒]
  ② SequenceMatcher(ASR文字, TTS文字).ratio() >= 0.5
     OR ASR文字 ∈ TTS文字（子串包含）

设计原则：
  - 宁可误判（把真人当回声），也要防止死循环
  - 4秒余量 = 最长 TTS 播放延迟 + 麦克风拾音延迟
  - 0.5 相似度阈值 = 允许 ASR 噪声偏差（部分识别）
```

### 第二重：多麦去重 `is_duplicate_llm()`

```
触发条件：
  同一 speaker_id 在 5 秒内，
  同一段话被多个麦克风（PC-Mic + ESP32）各自识别出来，
  文本相似度 >= 0.6 或互为子串

只有第一个到达的才会触发 LLM，其余静默丢弃
```

### 第三重：声纹门控

```
只有 speaker.id 非空（已注册用户）才能触发 LLM
unknown 用户的语音只做 ASR，不进 LLM
→ TTS 播放的声音不是注册用户，第三重天然过滤
```

**三重机制优先级**：回声检测 > 声纹门控 > 多麦去重

---

## 9. 设备注册与广播系统

```
_connected_devices: dict[str, WebSocket]
key = device_id (来自 ws://host:8765?device=xxx 查询参数)

常见 device_id：
  "PC-Mic"    ← mic-service Docker 容器
  "esp32-01"  ← ESP32 固件中的 DEVICE_ID 宏

广播模式：
  device="*"        → 广播给所有设备
  device="esp32*"   → 前缀匹配（所有 ESP32）
  device="esp32-01" → 单播

新设备连接时的 TTS 补发逻辑：
  _last_tts 记录最近一次 TTS（有效期 5 分钟）
  新 WebSocket 建立后立即推送 → 解决"ESP32 重启后错过 TTS"问题
```

---

## 10. 参数调优速查

### VAD 灵敏度

| 场景 | VAD_THRESHOLD | MIN_SILENCE_MS | 效果 |
|------|---------------|----------------|------|
| 安静房间 | 0.3~0.4 | 200 | 响应更灵敏 |
| **默认** | **0.5** | **300** | 平衡 |
| 嘈杂环境 | 0.7 | 500 | 减少误触 |

配置位置：`services/vad-service/docker-compose.yml` 的 `environment`。

### 声纹识别阈值

| 阈值 | 效果 |
|------|------|
| 0.3 以下 | 误识别率高（不推荐） |
| **0.55（默认）** | 平衡 |
| 0.7 以上 | 严格，但容易识别为 unknown |

配置位置：`services/voiceprint-service/voiceprint.yaml` → `voiceprint.similarity_threshold`

### TTS 回声检测窗口

| 参数 | 默认 | 含义 |
|------|------|------|
| 时间余量 | 4.0s | TTS 结束后还保持抑制多久 |
| 文本相似度 | 0.5 | 越低越严格（容易误判真人） |

配置位置：`services/vad-service/app.py` → `is_tts_echo()` 函数体。

### LLM 对话历史长度

```python
# services/llm-service/app.py
histories: dict[str, deque] = defaultdict(lambda: deque(maxlen=20))
# 20 = 10轮对话（user+assistant各1）
# 增大 → 上下文更长，Token 消耗更多
# 减小 → 省钱，但记忆更短
```

### TTS 音色切换

```bash
# 修改 docker-compose.yml 中的环境变量
TTS_VOICE_ID=presenter_male  # 主播男声
TTS_VOICE_ID=female-shaonv   # 少女音（默认）
TTS_VOICE_ID=male-qn-badao   # 霸道男声
```
