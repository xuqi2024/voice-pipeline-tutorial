# 实时语音处理管道教程

> 基于 Silero-VAD + FunASR + CAM++ 声纹 + MiniMax LLM + Edge-TTS 的完整实时语音智能体

---

## 🏗️ 架构总览

```
┌─────────────────────────────────────────────────────────────────────┐
│                          音频输入层                                   │
│                                                                       │
│   USB 麦克风 (UGREEN)          ESP32-S3 + INMP441                    │
│   mic-service :8001            WebSocket 直连                         │
│        │                              │                               │
│        └──────────────┬───────────────┘                               │
│                        │  PCM 16kHz 16bit mono (WebSocket)            │
└────────────────────────┼─────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    VAD Service :8765 (WebSocket)                      │
│                     Silero-VAD 语音活动检测                            │
│                                                                       │
│  接收多路音频流 → 检测人声起止 → 提取语音段 → 并行分发                  │
│                                                                       │
│  guard 1: 声纹是否匹配注册用户                                         │
│  guard 2: 是否为 TTS 回声（文本相似度 + 时间窗口）                      │
│  guard 3: 多麦克风重复发言去重（5s 窗口）                               │
└──────┬──────────────────────────┬────────────────────────────────────┘
       │ 语音 PCM                  │ 语音 PCM（并行）
       ▼                          ▼
┌──────────────┐        ┌──────────────────────┐
│ FunASR :10095│        │ voiceprint-api :8005  │
│  WebSocket   │        │  CAM++ 声纹模型        │
│  中文实时 ASR │        │  + MySQL 嵌入存储      │
│  Paraformer  │        │  多样本加权平均         │
└──────┬───────┘        └──────────┬────────────┘
       │ 识别文本                   │ 说话人 ID + 置信度
       └──────────────┬────────────┘
                      │
                      ▼（通过3层过滤后）
┌─────────────────────────────────────────────────────────────────────┐
│                    LLM Service :8006                                  │
│                    MiniMax M2.7                                        │
│                                                                       │
│  → 构建携带说话人身份的 Prompt                                          │
│  → 调用 MiniMax Anthropic 兼容接口                                     │
│  → 回复推送 Dashboard + 调用 TTS                                       │
└──────┬──────────────────────────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    TTS Service :8766                                   │
│              Edge-TTS（免费）/ MiniMax Speech-2.8-HD                  │
│                                                                       │
│  生成 WAV → 提供 HTTP 下载地址                                          │
│                                                                       │
│  VAD 广播 tts_url → ESP32 下载播放（I2S MAX98357A）                    │
│  Dashboard SSE   → 浏览器 AudioContext 播放                            │
└─────────────────────────────────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Dashboard :8080                                     │
│              实时事件日志 + 声纹管理 + AI 回复展示                       │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 📋 服务清单

| 服务 | 容器名 | 端口 | 说明 |
|------|--------|------|------|
| mic-service | mic-service | 8001 | USB 麦克风采集，推 PCM 到 VAD |
| VAD Service | vad-service | 8765 (WS) | Silero-VAD 检测 + 多路管道枢纽 |
| FunASR | funasr | 10095 (WS) | 中文实时语音识别（Paraformer） |
| voiceprint-api | voiceprint-api | 8005 (HTTP) | CAM++ 声纹注册/识别 |
| MySQL | voiceprint-mysql | 3306 | 声纹向量持久化存储 |
| LLM Service | llm-service | 8006 (HTTP) | MiniMax M2.7 对话 |
| TTS Service | tts-service | 8766 (HTTP) | Edge-TTS / MiniMax TTS |
| Dashboard | dashboard | 8080 (HTTP) | Web 控制台 |
| **ESP32-S3** | — | — | 端侧麦克风 + 喇叭（可选硬件） |

所有容器均在 `voice-pipeline` Docker 网络中互通。

---

## 🔄 完整数据流

```
1. 用户说话
   └→ mic-service（或 ESP32）采集 PCM 16kHz
      └→ WebSocket 推流到 vad-service:8765?device=<设备名>

2. VAD 检测到人声
   └→ 并行发给 FunASR（ASR）和 voiceprint-api（声纹）

3. 声纹识别返回：说话人 = "徐琪"，分数 = 0.82
   ASR 返回："你好，最近天气怎么样"

4. VAD 三层过滤：
   ✓ 声纹匹配注册用户
   ✓ 不是 TTS 回声（文本相似度检测）
   ✓ 5s 内无其他麦克风识别到相同内容
   └→ 调用 LLM Service

5. LLM Service：
   └→ 发送给 MiniMax M2.7："[徐琪] 你好，最近天气怎么样"
      └→ 回复："你好徐琪！最近天气..."
         └→ 推送 Dashboard（SSE tts_ready）
         └→ 调用 TTS Service 生成 WAV
         └→ 通过 VAD 广播 tts_url 给所有设备
            ├→ ESP32 下载 WAV → I2S 播放喇叭
            └→ Dashboard → AudioContext 播放
```

---

## 🚀 快速部署（新机器）

详见 **[docs/00-quick-deploy.md](docs/00-quick-deploy.md)**，完整的从零部署步骤。

**简版（已安装 Docker 的机器）：**

```bash
git clone https://github.com/xuqi2024/voice-pipeline-tutorial.git
cd voice-pipeline-tutorial

# 1. 修改你的配置（IP + API Key）
nano services/llm-service/docker-compose.yml   # HOST_IP + MINIMAX_API_KEY

# 2. 创建网络
docker network create voice-pipeline

# 3. 按顺序启动
for svc in funasr-service voiceprint-service tts-service llm-service vad-service dashboard mic-service; do
  echo "启动 $svc ..."
  cd services/$svc && docker compose up -d --build && cd ../..
  sleep 5
done

# 4. 检查
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

# 5. 打开 Dashboard
echo "访问: http://$(hostname -I | awk '{print $1}'):8080"
```

---

## 🔑 关键配置项

| 配置 | 文件 | 说明 |
|------|------|------|
| `MINIMAX_API_KEY` | `llm-service/docker-compose.yml` | MiniMax API 密钥 |
| `HOST_IP` | `llm-service/docker-compose.yml` | 本机 IP（ESP32/浏览器下载 TTS 用） |
| `VOICEPRINT_API_KEY` | `voiceprint-service/voiceprint.yaml` | 声纹 API Token |
| `similarity_threshold` | `voiceprint-service/voiceprint.yaml` | 声纹匹配阈值（推荐 0.55） |
| `MIC_KEYWORD` | `mic-service/docker-compose.yml` | USB 麦克风设备名关键词 |
| `WIFI_SSID/PASS` | `esp32-client/main/wifi_config.h` | ESP32 WiFi 配置 |
| `VAD_WS_URL` | `esp32-client/main/wifi_config.h` | ESP32 连接的 VAD 地址 |

---

## 📡 API 速查

| 接口 | 地址 | 说明 |
|------|------|------|
| Dashboard | `http://<IP>:8080` | Web 控制台 |
| VAD WebSocket | `ws://<IP>:8765?device=<id>` | 音频推流入口 |
| FunASR | `ws://<IP>:10095` | ASR（VAD 内部调用） |
| 声纹列表 | `GET :8005/voiceprint/speakers` | 列出已注册说话人 |
| 声纹注册 | `POST :8005/voiceprint/register` | 注册/追加样本 |
| 声纹识别 | `POST :8005/voiceprint/identify` | 识别说话人 |
| TTS 合成 | `POST :8766/tts` | 文本转语音 |
| TTS 音色列表 | `GET :8766/voices` | 可用音色 |
| LLM 健康 | `GET :8006/health` | 服务状态 |

---

## 📁 目录结构

```
voice-pipeline-tutorial/
├── start-all.sh                  # 一键启动脚本
├── stop-all.sh                   # 一键停止脚本
├── docs/
│   ├── 00-quick-deploy.md        # 新机器快速部署
│   ├── 01-overview.md            # 架构原理详解
│   ├── 02-audio-capture.md       # 音频采集说明
│   ├── 03-vad-service.md         # VAD 服务说明
│   ├── 04-funasr-service.md      # FunASR 说明
│   ├── 05-voiceprint-service.md  # 声纹服务说明
│   ├── 06-3d-speaker-explained.md # CAM++/3D-Speaker 原理
│   ├── 07-testing-guide.md       # 测试指南
│   ├── 08-logging-guide.md       # 日志查看
│   └── 09-tts-service.md         # TTS 服务说明
├── services/
│   ├── funasr-service/           # FunASR 官方镜像配置
│   ├── voiceprint-service/       # CAM++ 声纹 API（含补丁）
│   ├── vad-service/              # Silero-VAD + 管道协调
│   ├── mic-service/              # PC USB 麦克风采集服务
│   ├── llm-service/              # MiniMax LLM + TTS 调度
│   ├── tts-service/              # Edge-TTS / MiniMax TTS
│   ├── dashboard/                # Web 控制台（FastAPI + HTML）
│   └── esp32-client/             # ESP32-S3 固件（ESP-IDF v5.4）
├── tests/
│   ├── run_all_tests.sh
│   ├── test_vad.py
│   ├── test_funasr.py
│   └── test_voiceprint.py
└── client/                       # 浏览器端测试工具
```

---

## 🛡️ 声纹提高精度技巧

1. **首次注册**：安静环境，正常说话语速，距麦克风 30~50cm，录 5~8 秒
2. **追加样本**：在 Dashboard 声纹管理面板，点击"＋样本"追加 2~3 次不同场景录音（站着/坐着/不同距离），系统自动加权平均
3. **调整阈值**：样本充足后可在 `voiceprint.yaml` 中把 `similarity_threshold` 调高至 0.60 减少误识
4. **回声防误识**：VAD 自动检测 TTS 播放内容，已播出的文本在时间窗口内不会触发 LLM

---

## 🔧 开发说明

### 技术选型

| 组件 | 选型 | 理由 |
|------|------|------|
| 语音活动检测 | [Silero-VAD](https://github.com/snakers4/silero-vad) | 轻量、CPU 可用、延迟低 |
| 语音识别 | [FunASR Paraformer](https://github.com/modelscope/FunASR) | 中文效果优秀，支持实时流式 |
| 声纹识别 | [CAM++ (voiceprint-api)](https://github.com/xinnan-tech/voiceprint-api) | 基于 3D-Speaker，中文场景精度高 |
| LLM | MiniMax M2.7 | Anthropic 兼容接口，中文效果好 |
| TTS | Edge-TTS（默认）/ MiniMax Speech-2.8-HD | Edge 免费，MiniMax 更自然 |
| 协调层 | 自研 VAD Service | 多路音频、三层过滤、设备注册 |

---

## 📊 端口一览

```
8080   Dashboard（浏览器访问）
8765   VAD WebSocket（音频推流）
8001   mic-service HTTP（健康检查）
8005   voiceprint-api（声纹 REST）
8006   LLM Service（对话 REST）
8766   TTS Service（合成 REST）
10095  FunASR WebSocket（ASR）
3306   MySQL（voiceprint 内部）
```
