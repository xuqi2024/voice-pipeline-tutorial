# 麦克风采集与 WebSocket 传输

## 硬件确认

```bash
# 查看 USB 设备
lsusb | grep "0c45:6369"
# 预期输出: Bus 005 Device 002: ID 0c45:6369 Microdia UGREEN Camera

# 查看 ALSA 音频设备
arecord -l
# 预期: 找到 USB Audio Device

# 测试录音 (3秒)
arecord -D hw:1,0 -f S16_LE -r 16000 -c 1 -d 3 /tmp/test.wav
aplay /tmp/test.wav
```

## 找到正确的设备索引

```python
# 运行此脚本查看所有音频设备（使用 sounddevice 库）
import sounddevice as sd
devices = sd.query_devices()
for i, d in enumerate(devices):
    if d['max_input_channels'] > 0:
        print(f"[{i}] {d['name']} (输入通道: {d['max_input_channels']})")
```

> **注意**：同一个设备在**主机**和 **Docker 容器内**的索引可能不同！
> mic-service 通过 `MIC_KEYWORD=UGREEN` 环境变量按名称自动匹配，无需手动指定索引。

## 客户端安装

```bash
cd client
pip install -r requirements.txt
# 依赖: sounddevice==0.4.6, websockets, numpy
```

## 启动麦克风客户端

```bash
# 基本用法 (自动检测麦克风)
python mic_capture.py

# 指定设备索引
python mic_capture.py --device 1

# 指定 VAD 服务地址
python mic_capture.py --url ws://localhost:8765

# 调试模式 (显示音量和发送统计)
python mic_capture.py --debug
```

## 客户端工作原理

```
PyAudio 回调
    │ 每 512 样本触发一次 (32ms)
    │ 格式: bytes (int16 LE)
    ▼
WebSocket 发送队列
    │ asyncio Queue，避免阻塞
    ▼
WebSocket 客户端
    │ ws://localhost:8765
    │ 发送: binary (raw PCM bytes)
    ▼
VAD Service
```

## 音频参数说明

| 参数 | 值 | 原因 |
|------|----|------|
| 采样率 | 16000 Hz | Silero VAD / FunASR 要求 |
| 位深 | 16-bit signed | 标准格式，兼容性最好 |
| 声道数 | 1 (Mono) | 减少数据量，声纹识别要求 |
| 块大小 | 512 samples | Silero VAD 推荐的窗口大小 |
| 每块时长 | 32ms | 512/16000 = 0.032s |

## 故障排查

### 找不到麦克风设备

```bash
# 检查内核是否识别 USB 音频
dmesg | grep -i audio
dmesg | grep -i "usb.*sound\|sound.*usb"

# 重新插拔后检查
udevadm monitor --subsystem-match=sound
```

### 权限问题

```bash
# 将用户加入 audio 组
sudo usermod -aG audio $USER
# 重新登录后生效
```

### 音量太低

```bash
# 用 alsamixer 调节麦克风增益
alsamixer -c 1  # 替换 1 为你的声卡编号
# 选择 Mic 通道，按 ↑ 调高
```
