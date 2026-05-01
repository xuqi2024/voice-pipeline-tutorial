# voiceprint-api 部署

## 概述

[voiceprint-api](https://github.com/xinnan-tech/voiceprint-api) 是基于 **3D-Speaker** 模型的声纹识别 REST 服务，用于识别说话人身份。

## 部署步骤

### 1. 启动服务（含 MySQL）

```bash
cd services/voiceprint-service
docker compose up -d

# 查看日志
docker compose logs -f voiceprint-api
```

等待看到：
```
INFO: Application startup complete.
INFO: Uvicorn running on http://0.0.0.0:8005
```

### 2. 初始化数据库

首次启动时，MySQL 会自动初始化（通过 init SQL 文件）。

```bash
# 确认数据库就绪
docker exec voiceprint-mysql mysql -uroot -pvoiceprint123 \
    -e "SELECT COUNT(*) FROM voiceprint_db.voiceprints;"
```

### 3. 验证 API

```bash
# API Key（在 voiceprint.yaml 中配置）
API_KEY="de395e06-035c-44f9-9a6b-8ef126a8bea0"

# 健康检查（使用 key 查询参数）
curl "http://localhost:8005/voiceprint/health?key=${API_KEY}"
# 预期: {"status":"healthy"}
```

## API 接口说明

> **认证方式**：所有接口需要 Bearer Token 认证。
> 健康检查用 `?key=<token>` 查询参数，其余接口用 `Authorization: Bearer <token>` Header。

```bash
API_KEY="de395e06-035c-44f9-9a6b-8ef126a8bea0"
```

### 注册声纹

```bash
# 准备一段说话人的音频（WAV, 16kHz, mono, 至少3秒）
# 字段名: file（注意不是 audio）
curl -X POST http://localhost:8005/voiceprint/register \
  -H "Authorization: Bearer ${API_KEY}" \
  -F "speaker_id=张三" \
  -F "file=@/path/to/speaker.wav"
```

响应：
```json
{"status": "success", "speaker_id": "张三", "message": "声纹注册成功"}
```

### 识别说话人

```bash
# speaker_ids: 候选列表，逗号分隔（留空表示在所有已注册人员中识别）
# 字段名: file（注意不是 audio）
curl -X POST http://localhost:8005/voiceprint/identify \
  -H "Authorization: Bearer ${API_KEY}" \
  -F "speaker_ids=张三,李四" \
  -F "file=@/path/to/unknown.wav"
```

响应：
```json
{
  "speaker_id": "张三",
  "score": 0.923,
  "threshold": 0.7,
  "is_identified": true
}
```

score < threshold 时，`is_identified=false`，`speaker_id=null`

### 删除声纹

```bash
curl -X DELETE http://localhost:8005/voiceprint/张三 \
  -H "Authorization: Bearer ${API_KEY}"
```

## 注册声纹的最佳实践

1. **音频时长**: 建议 3~10 秒
2. **音频质量**: 安静环境录制，无背景噪声
3. **多次注册**: 同一人可多次注册，系统取平均特征向量
4. **更新声纹**: 删除旧的，重新注册

```bash
# 使用本地麦克风录制声纹 (需要 sox)
rec -r 16000 -c 1 -b 16 -e signed-integer speaker_zhangsan.wav trim 0 5

# 或使用 arecord
arecord -f S16_LE -r 16000 -c 1 -d 5 speaker_zhangsan.wav
```

## 阈值调优

| score 范围 | 含义 | threshold 建议 |
|-----------|------|---------------|
| 0.9+ | 非常确信 | 适合高安全场景 |
| 0.7~0.9 | 较高置信 | 默认推荐 |
| 0.5~0.7 | 一般匹配 | 嘈杂环境 |
| < 0.5 | 不匹配 | - |

修改阈值：在 `voiceprint.yaml` 中设置 `threshold: 0.7`

## 故障排查

```bash
# MySQL 连接失败
docker compose logs mysql
# 等待 MySQL 完全启动（约30秒）

# 模型下载慢
docker compose logs voiceprint-api | grep "download\|model"
# 首次启动需要下载 3D-Speaker 模型 (~300MB)

# 声纹识别准确度低
# 原因: 录音质量差、环境噪声大、音频太短
# 解决: 重新录制高质量音频，至少5秒
```
