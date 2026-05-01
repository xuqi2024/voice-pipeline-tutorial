# 日志查看与调试指南

## 快速查看所有服务日志

```bash
# 查看所有服务状态（一览表）
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

# 示例输出：
# NAMES              STATUS                   PORTS
# mic-service        Up 5 minutes (healthy)
# vad-service        Up 5 minutes (healthy)   0.0.0.0:8765->8765/tcp
# funasr             Up 5 minutes             0.0.0.0:10095->10095/tcp
# voiceprint-api     Up 5 minutes (healthy)   0.0.0.0:8005->8005/tcp
# voiceprint-mysql   Up 5 minutes (healthy)   3306/tcp
```

---

## 各服务日志命令

### 🎙️ 麦克风服务（mic-service）

```bash
# 实时跟踪日志（Ctrl+C 退出）
docker logs -f mic-service

# 查看最后 50 行
docker logs mic-service --tail 50

# 正常工作时的输出：
# [MIC] 已发现设备: UGREEN Camera (index=5, channels=2)
# [MIC] 已连接 VAD: ws://vad-service:8765
# [MIC] 音量: ░░░░░░░░░░ (静音)
# [MIC] 音量: ████░░░░░░ (说话时)
```

音量条说明：
- `░░░░░░░░░░` — 静音，麦克风正常工作
- `████░░░░░░` — 检测到声音（40%音量）
- `██████████` — 响亮的声音（100%音量）

**排查要点**：若日志一直没有连接成功，先确认 vad-service 已启动。

---

### 🔊 VAD 服务（vad-service）

```bash
# 实时跟踪
docker logs -f vad-service

# 按时间过滤（最近 5 分钟）
docker logs vad-service --since 5m

# 只看错误信息
docker logs vad-service 2>&1 | grep -i "error\|exception\|traceback"
```

**日志解读**：

```
[VAD] Client connected: 172.18.0.x        ← mic-service 连接进来
[VAD] chunk=512 prob=0.02 → SILENCE        ← 静音，prob 接近 0
[VAD] chunk=512 prob=0.87 → SPEECH_START   ← 检测到人声！
[VAD] chunk=512 prob=0.95 → SPEAKING       ← 持续说话
[VAD] chunk=512 prob=0.01 → SILENCE(300ms) → SEND  ← 停顿后发出
[VAD] Speech: 1280ms, → FunASR + voiceprint ← 同时发给两个服务
[VAD] FunASR result: {"text":"你好","is_final":1}
[VAD] voiceprint result: {"speaker_id":"user1","score":0.93}
```

VAD 概率值说明：
- `prob < 0.3` — 明确静音
- `0.3 ~ 0.5` — 不确定（背景音/噪声）
- `prob > 0.5` — 判定为有人说话
- `prob > 0.8` — 明确的人声

---

### 🗣️ FunASR 服务

```bash
# 实时跟踪
docker logs -f funasr

# 查看最后 100 行（启动时日志较多）
docker logs funasr --tail 100

# 只看识别结果行
docker logs funasr 2>&1 | grep -i "result\|text\|asr"
```

**启动阶段日志**（首次启动需等待模型下载，约 2-5 分钟）：

```
# 模型下载阶段
Downloading model: paraformer-zh...
Downloading model: fsmn-vad...
Downloading model: ct-punc...

# 启动完成
FunASR server started, listening on port 10095
```

**处理阶段日志**：

```
new connection coming in ...
connection total: 1
decode result: 你好这是一个测试
```

> ⚠️ FunASR 启动时会下载 ONNX 模型（约 400MB），**第一次启动需要等待 2-5 分钟**。
> 使用 `docker logs funasr --tail 20 -f` 观察下载进度。

---

### 🔑 声纹识别服务（voiceprint-api）

```bash
# 实时跟踪
docker logs -f voiceprint-api

# 查看请求日志
docker logs voiceprint-api 2>&1 | grep "POST\|GET\|DELETE"
```

**启动日志**：

```
# 模型加载（首次约 30-60 秒）
Loading model: iic/speech_campplus_sv_zh-cn_3dspeaker_16k
Model loaded successfully.
Warming up model...
Warmup done.
INFO: Application startup complete.
INFO: Uvicorn running on http://0.0.0.0:8005
```

**请求日志**：

```
INFO: 172.18.0.x:PORT - "POST /voiceprint/register HTTP/1.1" 200 OK
INFO: 172.18.0.x:PORT - "POST /voiceprint/identify HTTP/1.1" 200 OK
```

---

### 🗄️ MySQL（voiceprint-mysql）

```bash
# 查看 MySQL 日志
docker logs voiceprint-mysql --tail 30

# 连接数据库查看声纹数据
docker exec -it voiceprint-mysql mysql \
  -uroot -pvoiceprint123 \
  -e "SELECT speaker_id, created_at FROM voiceprint_db.voiceprints;"
```

---

## 按目录查看（推荐工作流）

进入各服务目录后可以用 `docker compose` 命令：

```bash
# VAD 服务
cd services/vad-service
docker compose logs -f            # 实时日志
docker compose logs --tail 50     # 最后50行
docker compose ps                 # 查看状态

# voiceprint 服务
cd services/voiceprint-service
docker compose logs -f voiceprint-api   # 只看 API 日志
docker compose logs -f mysql            # 只看 MySQL 日志

# FunASR 服务
cd services/funasr-service
docker compose logs -f

# 麦克风服务
cd services/mic-service
docker compose logs -f
```

---

## 资源监控

```bash
# 实时资源占用（CPU/内存）
docker stats

# 快照（不刷新）
docker stats --no-stream

# 示例输出：
# NAME              CPU %    MEM USAGE / LIMIT    MEM %
# funasr            0.5%     1.2GiB / 16GiB       7.5%
# voiceprint-api    0.3%     800MiB / 16GiB       5.0%
# vad-service       0.1%     200MiB / 16GiB       1.3%
# mic-service       0.2%     50MiB / 16GiB        0.3%
# voiceprint-mysql  0.1%     300MiB / 16GiB       1.9%
```

---

## 全链路调试流程

当说话后没有看到识别结果时，按以下顺序排查：

```
1. 检查 mic-service 日志 → 有没有音量条？
   有: ████ → 声音传到了 VAD ✓
   没有: ░░░░ → 检查麦克风设备、音量

2. 检查 vad-service 日志 → prob 值多少？
   prob > 0.5 → VAD 触发 ✓
   prob < 0.3 → 麦克风音量太低，调高增益

3. 检查 funasr 日志 → 有没有 decode result?
   有 → ASR 正常 ✓
   没有 → 检查网络连通性: docker exec vad-service curl -s funasr:10095

4. 检查 voiceprint-api 日志 → 有没有 POST /voiceprint/identify?
   有 → 声纹识别正常 ✓
   没有 → 检查: docker exec vad-service curl -s http://voiceprint-api:8005/voiceprint/health?key=de395e06-035c-44f9-9a6b-8ef126a8bea0
```

---

## 常用一键命令

```bash
# 重启单个服务
docker restart vad-service
docker restart funasr
docker restart voiceprint-api

# 重启全部服务
bash stop-all.sh && bash start-all.sh

# 清理并重建（解决配置问题）
cd services/vad-service
docker compose down && docker compose up -d --build

# 查看某时间段日志（例如最近10分钟）
docker logs vad-service --since 10m

# 把日志保存到文件
docker logs funasr > /tmp/funasr.log 2>&1
```

---

## 日志级别调整

### VAD 服务开启详细日志

在 `services/vad-service/docker-compose.yml` 中添加环境变量：

```yaml
environment:
  - LOG_LEVEL=DEBUG     # 显示更多调试信息
  - VAD_THRESHOLD=0.5
```

### FunASR 日志过滤

FunASR 日志量较大，建议过滤关键词：

```bash
# 只看识别结果
docker logs -f funasr | grep "decode"

# 看连接事件
docker logs -f funasr | grep "connection"

# 看错误
docker logs -f funasr | grep -i "error\|fail"
```
