# 快速部署指南

在一台全新的 Ubuntu 机器上，从零部署完整的语音管道系统。

---

## 系统要求

| 项目 | 最低 | 推荐 |
|------|------|------|
| OS | Ubuntu 20.04 | Ubuntu 22.04 LTS |
| CPU | 4 核 | 8 核 |
| RAM | 8 GB | 16 GB |
| 磁盘 | 30 GB | 60 GB（模型缓存） |
| 网络 | 能访问 Docker Hub / ModelScope | — |

---

## 第一步：安装基础依赖

```bash
# 更新系统
sudo apt update && sudo apt upgrade -y

# 安装 Docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker          # 或重新登录使权限生效

# 验证
docker --version       # Docker version 24.x+
docker compose version # Docker Compose version v2.x+
```

---

## 第二步：克隆项目

```bash
git clone https://github.com/xuqi2024/voice-pipeline-tutorial.git
cd voice-pipeline-tutorial
```

---

## 第三步：配置密钥

### 3.1 MiniMax API Key（必填，LLM + TTS 使用）

在 [MiniMax 平台](https://www.minimaxi.com) 获取 API Key，然后：

```bash
# LLM 服务配置
nano services/llm-service/docker-compose.yml
# 修改 MINIMAX_API_KEY 的值

# TTS 服务（如要使用 MiniMax TTS 替代 Edge TTS）
# services/tts-service/docker-compose.yml 中填入同一个 Key
# 若只用 Edge TTS（免费），可跳过，TTS_BACKEND=edge 已是默认值
```

### 3.2 服务器 IP（必填）

```bash
# 获取本机局域网 IP
ip addr show | grep "inet " | grep -v 127.0.0.1

# 修改 LLM 服务中的 HOST_IP（用于 TTS 音频下载地址）
nano services/llm-service/docker-compose.yml
# 将 HOST_IP=192.168.1.8 改为你的实际 IP
```

### 3.3 Voiceprint API Token

Token 已预置在配置中（`de395e06-035c-44f9-9a6b-8ef126a8bea0`）。  
如需修改，编辑 `services/voiceprint-service/voiceprint.yaml` 中的 `server.authorization`，
并同步修改 `services/vad-service/docker-compose.yml` 和 `services/dashboard/docker-compose.yml` 中的 `VOICEPRINT_API_KEY`。

---

## 第四步：创建 Docker 共享网络

```bash
docker network create voice-pipeline
```

---

## 第五步：启动各服务（按顺序）

### 5.1 FunASR（中文语音识别）

```bash
cd services/funasr-service
docker compose up -d
cd ../..

# 首次启动需要下载模型（约 2~5 分钟）
docker logs funasr -f   # 看到 "start to listen" 即就绪
```

### 5.2 Voiceprint API（声纹识别）

```bash
cd services/voiceprint-service
docker compose up -d --build
cd ../..

# 首次构建需要下载 CAM++ 模型（约 1~3 分钟）
docker logs voiceprint-api -f   # 看到 "Application startup complete" 即就绪
```

### 5.3 TTS 服务

```bash
cd services/tts-service
docker compose up -d --build
cd ../..

curl http://localhost:8766/health   # 返回 {"status":"ok"} 即就绪
```

### 5.4 LLM 服务

```bash
cd services/llm-service
docker compose up -d --build
cd ../..

curl http://localhost:8006/health   # 返回 {"status":"ok"} 即就绪
```

### 5.5 VAD 服务

```bash
cd services/vad-service
docker compose up -d --build
cd ../..

# 验证 WebSocket 端口
curl -s --max-time 2 http://localhost:8765/ || echo "WebSocket 端口 8765 已开放"
```

### 5.6 Dashboard

```bash
cd services/dashboard
docker compose up -d --build
cd ../..

curl http://localhost:8080/api/health   # 返回 {"status":"ok"} 即就绪
# 浏览器访问 http://<你的IP>:8080
```

### 5.7 PC 麦克风服务（如有 USB 麦克风）

```bash
# 确认麦克风设备存在
arecord -l   # 或 lsusb 查看

# 按实际情况修改 MIC_KEYWORD（设备名关键词）
nano services/mic-service/docker-compose.yml
# 例：MIC_KEYWORD=UGREEN 或 MIC_KEYWORD=USB

cd services/mic-service
docker compose up -d --build
cd ../..

docker logs mic-service -f   # 看到识别结果即正常
```

---

## 第六步：一键验证

```bash
bash tests/run_all_tests.sh
```

---

## 查看服务状态

```bash
# 全部容器状态
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

# 单服务日志（实时）
docker logs funasr -f
docker logs voiceprint-api -f
docker logs vad-service -f
docker logs mic-service -f
docker logs llm-service -f
docker logs tts-service -f
docker logs dashboard -f

# 检查网络连通性
docker network inspect voice-pipeline
```

---

## 停止所有服务

```bash
bash stop-all.sh
# 或逐个停止
for svc in mic-service vad-service llm-service tts-service dashboard; do
  cd services/$svc && docker compose down && cd ../..
done
cd services/voiceprint-service && docker compose down && cd ../..
cd services/funasr-service && docker compose down && cd ../..
```

---

## ESP32 开发板（可选）

如果你有 ESP32-S3 开发板（接 INMP441 麦克风 + MAX98357A 功放）：

### 安装 ESP-IDF v5.4

```bash
mkdir -p ~/esp && cd ~/esp
git clone --recursive https://github.com/espressif/esp-idf.git -b v5.4 v5.4.3
cd v5.4.3
./install.sh esp32s3
```

### 配置并编译

```bash
cd voice-pipeline-tutorial/services/esp32-client/main

# 复制配置模板
cp wifi_config.h.example wifi_config.h

# 修改 WiFi 和服务器 IP
nano wifi_config.h
# WIFI_SSID   "你的WiFi名"
# WIFI_PASS   "WiFi密码"
# VAD_WS_URL  "ws://192.168.x.x:8765?device=esp32-01"
# TTS_BASE_URL "http://192.168.x.x:8766"
```

```bash
cd ..   # 回到 esp32-client 目录
source ~/esp/v5.4.3/export.sh
idf.py set-target esp32s3
idf.py build
idf.py -p /dev/ttyUSB0 flash monitor
```

---

## 常见问题

### FunASR 无法连接

```bash
docker exec vad-service bash -c "nc -zv funasr 10095 && echo OK"
docker logs funasr --tail 30
```

### 声纹识别总是失败

- 检查 `voiceprint.yaml` 的 `similarity_threshold`（推荐 0.55）
- 多录几次样本，每次勾选"追加样本"累积平均
- 确认录音时距麦克风 30~50cm，安静环境

### 麦克风找不到

```bash
# 容器内列出音频设备
docker exec -it mic-service python -c "import sounddevice as sd; print(sd.query_devices())"
# 修改 docker-compose.yml 中的 MIC_KEYWORD 匹配实际设备名
```

### 端口冲突

```bash
ss -tlnp | grep -E "8765|8005|8080|8006|8766|10095"
# 如有冲突，修改对应服务 docker-compose.yml 中的 ports
```
