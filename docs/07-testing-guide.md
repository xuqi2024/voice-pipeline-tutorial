# 测试与验证指南

## 验证策略

每个服务都有独立的测试脚本，可以**单点验证**。

```
测试顺序（推荐）:
1. FunASR 服务验证      ← 不依赖其他服务
2. voiceprint-api 验证  ← 不依赖其他服务  
3. VAD 服务验证         ← 依赖以上两个服务
4. 全链路验证           ← 全部服务就绪后
```

## 单点测试

### 测试 FunASR

```bash
# 使用示例音频文件测试
python tests/test_funasr.py

# 使用自定义音频文件测试
python tests/test_funasr.py --audio /path/to/audio.wav

# 预期输出：
# [✓] FunASR WebSocket 连接成功
# [✓] 发送音频数据 (xxx bytes)
# [✓] 收到实时结果: "你好"
# [✓] 收到最终结果: "你好，这是测试。"
# [PASS] FunASR 服务验证通过
```

### 测试 voiceprint-api

```bash
python tests/test_voiceprint.py

# 预期输出：
# [✓] 健康检查通过 (HTTP 200)
# [✓] 声纹注册成功: test_speaker_1
# [✓] 声纹识别成功: test_speaker_1 (score: 0.98)
# [✓] 声纹删除成功: test_speaker_1
# [PASS] voiceprint-api 验证通过
```

### 测试 VAD 服务

```bash
python tests/test_vad.py

# 预期输出：
# [✓] WebSocket 连接成功 (ws://localhost:8765)
# [✓] 发送静音数据，无检测结果（符合预期）
# [✓] 发送语音数据
# [✓] 收到 VAD 事件: speech_start
# [✓] 收到 VAD 事件: speech_end
# [✓] 收到最终结果包含: text, speaker
# [PASS] VAD 服务验证通过
```

### 全链路测试

```bash
bash tests/run_all_tests.sh

# 预期输出：
# ════════════════════════════════════
# 🧪 语音管道全链路测试
# ════════════════════════════════════
# [1/3] 测试 FunASR...         [PASS]
# [2/3] 测试 voiceprint-api... [PASS]
# [3/3] 测试 VAD...            [PASS]
# ════════════════════════════════════
# ✅ 所有服务验证通过！
# 管道已就绪，可以启动麦克风客户端
# ════════════════════════════════════
```

## 交互式测试

### 麦克风实时测试

```bash
# 启动麦克风客户端（调试模式）
python client/mic_capture.py --debug

# 对着麦克风说话，观察输出：
# [MIC] 音量: ████░░░░ (40%)
# [VAD] 检测到语音...
# [ASR] 实时结果: "你好"
# [ASR] 最终结果: "你好，这是一个测试。"
# [声纹] 识别结果: 张三 (score: 0.91)
```

### 手动注册声纹后测试

```bash
# 1. 录制自己的声音（5秒）
arecord -f S16_LE -r 16000 -c 1 -d 5 my_voice.wav

API_KEY="de395e06-035c-44f9-9a6b-8ef126a8bea0"

# 2. 注册声纹（注意字段名是 file，需要 Bearer Token）
curl -X POST http://localhost:8005/voiceprint/register \
  -H "Authorization: Bearer ${API_KEY}" \
  -F "speaker_id=我的名字" \
  -F "file=@my_voice.wav"

# 3. 再次运行麦克风客户端，说话后应识别出你的名字
cd services/mic-service && docker compose up -d
```

## 检查服务状态

```bash
# 查看所有服务状态
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

# 查看资源占用
docker stats --no-stream

# 查看日志（最后50行）
docker logs vad-service --tail 50
docker logs funasr --tail 50
docker logs voiceprint-api --tail 50
```

## 常见问题排查

| 症状 | 可能原因 | 排查命令 |
|------|---------|---------|
| test_funasr 超时 | FunASR 未就绪 | `docker logs funasr \| tail -20` |
| 声纹识别 score 很低 | 录音质量差 | 在安静环境重新录制 |
| VAD 从不触发 | 阈值太高/麦克风音量低 | 调低 `VAD_THRESHOLD=0.3` |
| VAD 一直触发 | 背景噪声大 | 调高 `VAD_THRESHOLD=0.7` |
| WebSocket 断连 | 网络问题或服务崩溃 | 检查容器状态和日志 |
