#!/usr/bin/env python3
"""
麦克风采集客户端

采集 USB 麦克风音频并实时发送到 VAD WebSocket 服务。
设备: Bus 005 Device 002: ID 0c45:6369 Microdia UGREEN Camera

用法:
  python mic_capture.py                          # 自动检测麦克风
  python mic_capture.py --device 1               # 指定设备索引
  python mic_capture.py --url ws://host:8765     # 指定 VAD 服务地址
  python mic_capture.py --debug                  # 调试模式（显示音量和统计）
  python mic_capture.py --list-devices           # 列出所有音频输入设备
"""

import argparse
import asyncio
import json
import logging
import os
import struct
import sys
import time
from queue import Queue, Empty

import numpy as np
import sounddevice as sd
import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MIC] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# 音频参数（与 VAD 服务保持一致）
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_SAMPLES = 512      # 每块采样数 (32ms)
CHUNK_BYTES = CHUNK_SAMPLES * 2  # 每块字节数（16-bit = 2 bytes/sample）

# 支持环境变量配置（Docker 服务模式）
_ENV_DEVICE = os.environ.get("MIC_DEVICE_INDEX")
_ENV_URL = os.environ.get("VAD_WS_URL", "ws://vad-service:8765")
_ENV_DEBUG = os.environ.get("MIC_DEBUG", "").lower() in ("1", "true", "yes")


def find_microphone_device(keyword: str = None) -> int:
    """查找麦克风设备索引，优先匹配 keyword"""
    devices = sd.query_devices()
    input_devs = [(i, d) for i, d in enumerate(devices) if d["max_input_channels"] > 0]

    if not input_devs:
        raise RuntimeError("未找到任何音频输入设备")

    if keyword:
        for idx, d in input_devs:
            if keyword.lower() in d["name"].lower():
                logger.info(f"找到匹配麦克风 [{idx}]: {d['name']}")
                return idx

    idx, d = input_devs[0]
    logger.info(f"使用默认麦克风 [{idx}]: {d['name']}")
    return idx


def list_devices():
    """列出所有音频输入设备"""
    print("\n可用音频输入设备:")
    print("-" * 50)
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            print(f"  [{i}] {d['name']}")
            print(f"       采样率: {d['default_samplerate']:.0f} Hz")
            print(f"       输入通道: {d['max_input_channels']}")
    print("-" * 50)


def volume_bar(pcm_bytes: bytes, width: int = 20) -> str:
    """根据 RMS 生成音量条"""
    samples = struct.unpack(f"{len(pcm_bytes)//2}h", pcm_bytes)
    rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
    level = min(int(rms / 32768 * width * 5), width)
    return "█" * level + "░" * (width - level)


async def vad_receiver(websocket, debug: bool):
    """接收并打印 VAD 结果"""
    async for message in websocket:
        try:
            data = json.loads(message)
            text = data.get("text", "")
            speaker = data.get("speaker")

            if text or speaker:
                speaker_str = ""
                if speaker:
                    speaker_str = f" [{speaker['id']} ({speaker['score']:.2f})]"
                print(f"\n🎙️ 识别结果: {text}{speaker_str}")
        except json.JSONDecodeError:
            logger.warning(f"无法解析结果: {message}")


async def stream_microphone(
    device_index: int,
    vad_url: str,
    debug: bool = False,
):
    """采集麦克风并发送到 VAD 服务"""
    audio_queue: Queue = Queue(maxsize=100)

    def audio_callback(indata, frames, time_info, status):
        if status:
            logger.warning(f"音频状态: {status}")
        # indata is numpy array; convert to int16 bytes
        pcm = (indata[:, 0] * 32767).astype(np.int16).tobytes()
        audio_queue.put_nowait(pcm)

    logger.info(f"麦克风已启动，连接 VAD 服务: {vad_url}")
    print("开始录音，按 Ctrl+C 停止...\n")

    sent_chunks = 0
    start_time = time.time()

    try:
        async with websockets.connect(vad_url, open_timeout=10) as ws:
            logger.info("✅ 已连接到 VAD 服务")

            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="float32",
                blocksize=CHUNK_SAMPLES,
                device=device_index,
                callback=audio_callback,
            ):
                recv_task = asyncio.create_task(vad_receiver(ws, debug))

                while True:
                    try:
                        chunk = audio_queue.get_nowait()
                    except Empty:
                        await asyncio.sleep(0.001)
                        continue

                    await ws.send(chunk)
                    sent_chunks += 1

                    if debug and sent_chunks % 50 == 0:
                        elapsed = time.time() - start_time
                        bar = volume_bar(chunk)
                        print(
                            f"\r音量: {bar} ({sent_chunks} 块, {elapsed:.1f}s)",
                            end="",
                            flush=True,
                        )

    except websockets.exceptions.ConnectionClosed as e:
        logger.error(f"VAD 服务连接断开: {e}")
    except ConnectionRefusedError:
        logger.error(f"无法连接到 VAD 服务: {vad_url}")
        logger.error("请确认 VAD 服务已启动: cd services/vad-service && docker compose up -d")


def main():
    parser = argparse.ArgumentParser(description="麦克风 WebSocket 客户端")
    parser.add_argument("--device", type=int, default=None, help="音频设备索引")
    parser.add_argument("--url", default=None, help="VAD 服务地址")
    parser.add_argument("--debug", action="store_true", help="显示调试信息（音量条等）")
    parser.add_argument("--list-devices", action="store_true", help="列出音频设备后退出")
    args = parser.parse_args()

    if args.list_devices:
        list_devices()
        sys.exit(0)

    # 环境变量优先，命令行参数其次
    vad_url = args.url or _ENV_URL
    debug = args.debug or _ENV_DEBUG

    if args.device is not None:
        device_index = args.device
    elif _ENV_DEVICE is not None:
        device_index = int(_ENV_DEVICE)
    else:
        device_index = find_microphone_device(keyword="USB")

    d = sd.query_devices(device_index)
    logger.info(f"使用麦克风: [{device_index}] {d['name']}")

    try:
        asyncio.run(stream_microphone(device_index, vad_url, debug))
    except KeyboardInterrupt:
        print("\n\n已停止录音")


if __name__ == "__main__":
    main()
