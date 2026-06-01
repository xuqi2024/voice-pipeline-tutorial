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
import io
import json
import logging
import os
import struct
import subprocess
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from queue import Empty, Queue
from urllib.parse import parse_qs, urlparse

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
_ENV_KEYWORD = os.environ.get("MIC_KEYWORD", "USB")
_ENV_URL = os.environ.get("VAD_WS_URL", "ws://vad-service:8765")
_ENV_DEBUG = os.environ.get("MIC_DEBUG", "").lower() in ("1", "true", "yes")

# ──────────────────── 服务端录音 HTTP 服务 ────────────────────
MIC_HTTP_PORT = int(os.environ.get("MIC_HTTP_PORT", "8001"))

_record_lock = threading.Lock()
_record_chunks: list = []
_recording = False
_record_target_chunks = 0


def _build_wav(chunks: list) -> bytes:
    raw = b"".join(chunks)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(raw)
    return buf.getvalue()


class RecordHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 静默 HTTP 日志

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def do_GET(self):
        global _recording, _record_chunks, _record_target_chunks
        parsed = urlparse(self.path)
        if parsed.path != "/record":
            self.send_error(404)
            return

        qs = parse_qs(parsed.query)
        seconds = int(qs.get("seconds", ["5"])[0])
        seconds = max(1, min(seconds, 30))
        target = seconds * SAMPLE_RATE // CHUNK_SAMPLES

        with _record_lock:
            _record_chunks = []
            _record_target_chunks = target
            _recording = True

        logger.info(f"开始服务端录音 {seconds}s ({target} 块)...")
        deadline = time.time() + seconds + 1.0
        while _recording and time.time() < deadline:
            time.sleep(0.05)

        with _record_lock:
            _recording = False
            chunks = list(_record_chunks)

        wav = _build_wav(chunks)
        logger.info(f"录音完成: {len(chunks)} 块, {len(wav)} 字节")

        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(wav)


def start_record_http_server():
    server = HTTPServer(("0.0.0.0", MIC_HTTP_PORT), RecordHandler)
    logger.info(f"录音 HTTP 服务: http://0.0.0.0:{MIC_HTTP_PORT}/record?seconds=5")
    server.serve_forever()


def unmute_usb_mic(card: int = 1):
    """
    USB 麦克风在 Linux 上默认 Capture Switch 为 off，需要手动打开。
    通过 amixer 自动启用所有 USB 音频卡的 Capture Switch。
    """
    try:
        result = subprocess.run(
            ["amixer", "-c", str(card), "contents"],
            capture_output=True, text=True, timeout=3
        )
        for line in result.stdout.splitlines():
            if "Capture Switch" in line and "numid=" in line:
                numid = line.split("numid=")[1].split(",")[0]
                val_result = subprocess.run(
                    ["amixer", "-c", str(card), "cget", f"numid={numid}"],
                    capture_output=True, text=True, timeout=3
                )
                if ": values=off" in val_result.stdout:
                    subprocess.run(
                        ["amixer", "-c", str(card), "cset", f"numid={numid}", "on"],
                        capture_output=True, timeout=3
                    )
                    logger.info(f"已启用麦克风 Capture Switch (card={card}, numid={numid})")
    except Exception as e:
        logger.debug(f"unmute_usb_mic: {e}")


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


async def _connect_and_stream(ws, audio_queue: Queue, debug: bool):
    """在已建立的 WebSocket 连接上持续发送音频块，直到连接关闭。"""
    recv_task = asyncio.create_task(vad_receiver(ws, debug))
    sent_chunks = 0
    start_time = time.time()
    try:
        while True:
            try:
                chunk = audio_queue.get_nowait()
            except Empty:
                await asyncio.sleep(0.001)
                continue

            # 带超时的 send，防止在断开的连接上永久阻塞
            await asyncio.wait_for(ws.send(chunk), timeout=5.0)
            sent_chunks += 1

            if debug and sent_chunks % 50 == 0:
                elapsed = time.time() - start_time
                bar = volume_bar(chunk)
                print(f"\r音量: {bar} ({sent_chunks} 块, {elapsed:.1f}s)", end="", flush=True)
    finally:
        recv_task.cancel()
        try:
            await recv_task
        except asyncio.CancelledError:
            pass


async def stream_microphone(
    device_index: int,
    vad_url: str,
    debug: bool = False,
):
    """采集麦克风并持续发送到 VAD 服务，断线自动重连。"""
    # 队列调大，减少因 VAD 短暂慢速导致的丢帧
    audio_queue: Queue = Queue(maxsize=200)

    def audio_callback(indata, frames, time_info, status):
        if status:
            logger.warning(f"音频状态: {status}")
        pcm = (indata[:, 0] * 32767).astype(np.int16).tobytes()
        try:
            audio_queue.put_nowait(pcm)
        except Exception:
            pass  # 队列满时丢弃最新帧，保证回调不阻塞

        global _recording
        if _recording:
            with _record_lock:
                _record_chunks.append(pcm)
                if len(_record_chunks) >= _record_target_chunks:
                    _recording = False

    dev_info = sd.query_devices(device_index, kind="input")
    channels = min(dev_info["max_input_channels"], 2)
    logger.info(f"设备通道数: {channels}")
    logger.info(f"连接 VAD 服务: {vad_url}")
    print("开始录音，按 Ctrl+C 停止...\n")

    retry_delay = 3   # 初始重连等待秒数
    MAX_DELAY   = 60  # 最长重连等待秒数

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=channels,
        dtype="float32",
        blocksize=CHUNK_SAMPLES,
        device=device_index,
        callback=audio_callback,
    ):
        while True:
            try:
                async with websockets.connect(
                    vad_url, open_timeout=10, ping_interval=20, ping_timeout=10
                ) as ws:
                    logger.info("✅ 已连接到 VAD 服务")
                    retry_delay = 3  # 连接成功后重置退避
                    # 清空积压的旧音频帧，从当前时刻开始发送
                    while not audio_queue.empty():
                        try:
                            audio_queue.get_nowait()
                        except Empty:
                            break
                    await _connect_and_stream(ws, audio_queue, debug)

            except (websockets.exceptions.ConnectionClosed,
                    websockets.exceptions.InvalidHandshake,
                    websockets.exceptions.WebSocketException) as e:
                logger.warning(f"VAD 连接断开: {e}，{retry_delay}s 后重连...")
            except (ConnectionRefusedError, OSError) as e:
                logger.warning(f"VAD 不可达: {e}，{retry_delay}s 后重试...")
            except asyncio.TimeoutError:
                logger.warning(f"VAD send 超时，{retry_delay}s 后重连...")
            except Exception as e:
                logger.error(f"意外错误: {e}，{retry_delay}s 后重试...")

            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, MAX_DELAY)


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
        device_index = find_microphone_device(keyword=_ENV_KEYWORD)

    d = sd.query_devices(device_index)
    logger.info(f"使用麦克风: [{device_index}] {d['name']}")

    # Unmute USB mic capture switch (defaults to off on Linux)
    # Extract card number from device name like "hw:1,0"
    dev_name = d.get("name", "")
    import re
    card_match = re.search(r'hw:(\d+)', dev_name)
    if card_match:
        unmute_usb_mic(card=int(card_match.group(1)))

    try:
        # 启动服务端录音 HTTP 服务（后台线程）
        threading.Thread(target=start_record_http_server, daemon=True).start()
        asyncio.run(stream_microphone(device_index, vad_url, debug))
    except KeyboardInterrupt:
        print("\n\n已停止录音")


if __name__ == "__main__":
    main()
