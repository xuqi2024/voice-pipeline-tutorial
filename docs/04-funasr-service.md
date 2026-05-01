# FunASR 服务部署

## 概述

FunASR 是阿里达摩院开源的语音识别框架，本教程使用其**中文实时语音听写服务**。

- **模型**: Paraformer-large (中文，工业级)
- **模式**: 2pass (实时+离线校正)
- **协议**: WebSocket

## 部署步骤

### 1. 启动 FunASR Docker

```bash
cd services/funasr-service
docker compose up -d

# 首次启动会自动下载模型 (~2GB)，需要等待几分钟
# 查看进度
docker compose logs -f funasr
```

等待看到以下日志表示就绪：
```
[INFO] FunASR server started, listening on port 10095
```

### 2. 验证服务

```bash
# 检查端口
ss -tlnp | grep 10095

# 运行验证测试
python ../../tests/test_funasr.py
```

## 实时模式说明

FunASR 使用 **2pass 模式**：

```
音频流 ──→ 在线模型 ──→ 实时结果 (is_final=false)
                  │
                  └──→ 离线模型 ──→ 最终结果 (is_final=true)
```

- **实时结果**: 低延迟，可能有错误
- **最终结果**: 稍有延迟，更准确

### WebSocket 协议格式

**发送音频（握手）**:
```json
{
    "mode": "2pass",
    "chunk_size": [5, 10, 5],
    "chunk_interval": 10,
    "encoder_chunk_look_back": 4,
    "decoder_chunk_look_back": 0,
    "wav_name": "stream",
    "is_speaking": true,
    "hotwords": "",
    "itn": true
}
```

**发送音频数据**: Binary（PCM 字节）

**发送结束信号**:
```json
{"is_speaking": false}
```

**接收结果**:
```json
{
    "text": "你好，我想问一下",
    "mode": "2pass-online",
    "is_final": 0
}
// 最终结果:
{
    "text": "你好，我想问一下。",
    "mode": "2pass-offline",
    "is_final": 1
}
```

## 资源需求

| 资源 | 最低要求 | 推荐 |
|------|---------|------|
| CPU | 2 核 | 4 核 |
| RAM | 3 GB | 6 GB |
| 磁盘 | 5 GB (模型) | 10 GB |

## 模型存储

模型下载后存放在本机的 Docker volume：

```bash
# 查看模型 volume
docker volume ls | grep funasr
docker volume inspect funasr-service_funasr-models
```

模型位于容器内的 `/workspace/models/`：
- `speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch`
- `speech_fsmn_vad_zh-cn-16k-common-pytorch`
- `punc_ct-transformer_zh-cn-common-vocab272727-pytorch`

## 故障排查

```bash
# OOM (内存不足)
docker compose logs funasr | grep -i "oom\|killed\|memory"
# 解决：增加 docker memory limit 或减少 worker 数量

# 端口被占用
sudo ss -tlnp | grep 10095
# 解决：修改 docker-compose.yml 中的端口映射

# 模型下载失败 (网络问题)
# 解决：设置国内镜像源或手动下载模型
docker exec -it funasr bash
cd /workspace
# 手动执行下载脚本
```
