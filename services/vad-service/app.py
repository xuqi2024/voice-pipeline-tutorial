#!/usr/bin/env python3
"""
VAD Service - 语音活动检测服务

接收麦克风 PCM 音频流，检测人声片段，
并转发给 FunASR (ASR) 和 voiceprint-api (声纹识别)。

协议:
  - 输入: WebSocket binary (PCM int16 LE, 16kHz, mono, 512 samples/chunk)
  - 输出: WebSocket text (JSON 结果) 或无响应（静音时）
"""

import asyncio
import io
import json
import logging
import os
import struct
import time
import wave
from collections import deque
from typing import Optional

import httpx
import numpy as np
import torch
import websockets
from silero_vad import load_silero_vad

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [VAD] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ──────────────────────────── 配置 ────────────────────────────
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8765"))
SAMPLE_RATE = 16000
CHUNK_SAMPLES = 512  # Silero VAD 推荐窗口大小

VAD_THRESHOLD = float(os.getenv("VAD_THRESHOLD", "0.5"))
MIN_SPEECH_MS = int(os.getenv("MIN_SPEECH_MS", "250"))
MIN_SILENCE_MS = int(os.getenv("MIN_SILENCE_MS", "300"))
MAX_SPEECH_MS = int(os.getenv("MAX_SPEECH_MS", "10000"))

FUNASR_WS_URL = os.getenv("FUNASR_WS_URL", "ws://funasr:10095")
VOICEPRINT_API_URL = os.getenv("VOICEPRINT_API_URL", "http://voiceprint-api:8005")
VOICEPRINT_API_KEY = os.getenv("VOICEPRINT_API_KEY", "de395e06-035c-44f9-9a6b-8ef126a8bea0")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "")  # 留空则不推送
LLM_SERVICE_URL = os.getenv("LLM_SERVICE_URL", "")  # 留空则不调用 LLM
HTTP_API_PORT = int(os.getenv("HTTP_API_PORT", "8767"))  # 内部 HTTP API 端口

MIN_SILENCE_CHUNKS = int(MIN_SILENCE_MS / (CHUNK_SAMPLES / SAMPLE_RATE * 1000))
MIN_SPEECH_CHUNKS = int(MIN_SPEECH_MS / (CHUNK_SAMPLES / SAMPLE_RATE * 1000))
MAX_SPEECH_CHUNKS = int(MAX_SPEECH_MS / (CHUNK_SAMPLES / SAMPLE_RATE * 1000))

# 已连接的 WebSocket 设备注册表 { device_id: websocket }
_connected_devices: dict = {}

# 最近一次 TTS URL，用于新设备连接时立即推送（60s 有效）
_last_tts: dict = {}  # {"url": str, "ts": float}

# 已弃用：TTS 时间抑制（改用声纹过滤，不再需要）
_suppress_until: float = 0.0


def build_wav_bytes(pcm_chunks: list[bytes]) -> bytes:
    """将 PCM 块列表打包成 WAV 格式字节"""
    raw = b"".join(pcm_chunks)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(raw)
    return buf.getvalue()


async def push_event(event: dict):
    """推送管道事件到 Dashboard（fire-and-forget，失败不影响主流程）"""
    if not DASHBOARD_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.post(f"{DASHBOARD_URL}/api/vad-event", json=event)
    except Exception:
        pass  # dashboard 不可用时静默忽略


async def call_llm(text: str, speaker: str, device: str):
    """调用 LLM 服务生成回复（fire-and-forget）"""
    if not LLM_SERVICE_URL or not text:
        return
    try:
        async with httpx.AsyncClient(timeout=35.0) as client:
            await client.post(
                f"{LLM_SERVICE_URL}/api/chat",
                json={"text": text, "speaker": speaker, "device": device},
            )
    except Exception as e:
        logger.warning(f"LLM service call failed: {e}")


async def recognize_with_funasr(wav_bytes: bytes) -> Optional[str]:
    """通过 FunASR WebSocket 识别语音"""
    try:
        async with websockets.connect(
            FUNASR_WS_URL, open_timeout=5, close_timeout=5
        ) as ws:
            # 握手：发送配置
            await ws.send(json.dumps({
                "mode": "2pass",
                "chunk_size": [5, 10, 5],
                "chunk_interval": 10,
                "encoder_chunk_look_back": 4,
                "decoder_chunk_look_back": 0,
                "wav_name": "stream",
                "is_speaking": True,
                "hotwords": "",
                "itn": True,
            }))

            # 以 1600 字节 (50ms) 为单位发送 WAV 内容（跳过 WAV 头部 44 字节）
            audio_data = wav_bytes[44:]
            chunk_size = 3200  # 100ms chunks
            for i in range(0, len(audio_data), chunk_size):
                await ws.send(audio_data[i:i + chunk_size])
                await asyncio.sleep(0.05)

            # 通知 FunASR 说话结束
            await ws.send(json.dumps({"is_speaking": False}))

            # 收集结果，等待 is_final=1
            final_text = ""
            deadline = asyncio.get_event_loop().time() + 10
            while asyncio.get_event_loop().time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=3.0)
                    data = json.loads(msg)
                    text = data.get("text", "")
                    if text:
                        final_text = text
                    if data.get("is_final") == 1:
                        break
                except asyncio.TimeoutError:
                    break

            return final_text or None

    except Exception as e:
        logger.warning(f"FunASR 错误: {e}")
        return None


async def identify_speaker(wav_bytes: bytes) -> Optional[dict]:
    """通过 voiceprint-api 识别说话人"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{VOICEPRINT_API_URL}/voiceprint/identify",
                headers={"Authorization": f"Bearer {VOICEPRINT_API_KEY}"},
                files={"file": ("audio.wav", wav_bytes, "audio/wav")},
                data={"speaker_ids": ""},
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("speaker_id"):
                    return {
                        "id": data["speaker_id"],
                        "score": round(data["score"], 3),
                    }
    except Exception as e:
        logger.warning(f"voiceprint-api 错误: {e}")
    return None


async def handle_client(websocket):
    """处理单个 WebSocket 客户端连接"""
    addr = websocket.remote_address

    # 从 URL 查询参数中提取设备 ID，例如 ws://host:8765?device=esp32-01
    from urllib.parse import urlparse, parse_qs
    try:
        # websockets v12: use websocket.path (set during HTTP handshake)
        raw_path = getattr(websocket, "path", None) or getattr(websocket, "request_uri", "")
        qs = parse_qs(urlparse(raw_path).query)
        device_id = qs.get("device", [f"{addr[0]}"])[0]
    except Exception:
        device_id = str(addr[0])

    logger.info(f"客户端连接: {addr} device={device_id}")

    # 注册设备 WebSocket（覆盖旧连接，新连接优先）
    _connected_devices[device_id] = websocket

    # 若 60s 内有 TTS 未播放，立即推送给新连接的设备
    if _last_tts and (time.time() - _last_tts.get("ts", 0) < 60):
        try:
            await websocket.send(json.dumps({
                "type": "tts_url",
                "url": _last_tts["url"],
            }, ensure_ascii=False))
            logger.info(f"补发缓存 TTS → {device_id}: {_last_tts['url']}")
        except Exception:
            pass

    # 每个连接独立的 VAD 模型实例（线程安全）
    model = load_silero_vad()
    model.eval()

    # 状态机
    is_speaking = False
    silence_chunks = 0
    speech_chunks = 0
    speech_buffer: list[bytes] = []  # 积累的语音 PCM 块

    try:
        async for message in websocket:
            if not isinstance(message, bytes):
                continue

            # 每次只处理标准 512 个样本
            for offset in range(0, len(message), CHUNK_SAMPLES * 2):
                chunk = message[offset: offset + CHUNK_SAMPLES * 2]
                if len(chunk) < CHUNK_SAMPLES * 2:
                    continue

                # TTS 时间抑制已弃用（改用声纹过滤），此处保留短暂抑制作为保险
                # 转换为 float32 tensor [-1, 1]
                samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
                tensor = torch.from_numpy(samples)

                with torch.no_grad():
                    prob = model(tensor, SAMPLE_RATE).item()

                if not is_speaking:
                    if prob >= VAD_THRESHOLD:
                        is_speaking = True
                        speech_chunks = 1
                        silence_chunks = 0
                        speech_buffer = [chunk]
                        logger.info(f"▶ 检测到语音开始 [{device_id}] (prob={prob:.2f})")
                        asyncio.create_task(push_event({"type": "speech_start", "prob": round(prob, 3), "device": device_id}))
                else:
                    speech_buffer.append(chunk)
                    speech_chunks += 1

                    if prob < VAD_THRESHOLD:
                        silence_chunks += 1
                    else:
                        silence_chunks = 0

                    # 语音结束条件：静音超过阈值
                    if silence_chunks >= MIN_SILENCE_CHUNKS:
                        if speech_chunks >= MIN_SPEECH_CHUNKS:
                            duration_ms = speech_chunks * CHUNK_SAMPLES / SAMPLE_RATE * 1000
                            logger.info(f"■ 语音结束 [{device_id}] ({duration_ms:.0f}ms, {len(speech_buffer)} 块)")
                            asyncio.create_task(push_event({"type": "speech_end", "duration_ms": round(duration_ms), "device": device_id}))

                            wav_bytes = build_wav_bytes(speech_buffer)

                            # 并行调用 ASR 和声纹识别
                            asr_task = asyncio.create_task(recognize_with_funasr(wav_bytes))
                            vp_task = asyncio.create_task(identify_speaker(wav_bytes))
                            text, speaker = await asyncio.gather(asr_task, vp_task)

                            # 推送各自结果到 Dashboard
                            asyncio.create_task(push_event({"type": "asr_result", "text": text or "", "device": device_id}))
                            asyncio.create_task(push_event({
                                "type": "voiceprint_result",
                                "speaker": speaker["id"] if speaker else None,
                                "score": speaker["score"] if speaker else None,
                                "device": device_id,
                            }))

                            result = {
                                "text": text or "",
                                "speaker": speaker,
                            }
                            logger.info(f"结果 [{device_id}]: text={text!r} speaker={speaker}")
                            asyncio.create_task(push_event({
                                "type": "result",
                                "text": text or "",
                                "speaker": speaker["id"] if speaker else None,
                                "device": device_id,
                            }))
                            await websocket.send(json.dumps(result, ensure_ascii=False))

                            # 调用 LLM 生成智能回复
                            # 只有声纹识别到的注册用户才进入 LLM（TTS 播放的合成音不是注册用户，天然过滤）
                            if text and speaker and speaker.get("id"):
                                asyncio.create_task(call_llm(
                                    text,
                                    speaker["id"],
                                    device_id,
                                ))
                        else:
                            logger.debug("语音片段太短，忽略")

                        # 重置状态
                        is_speaking = False
                        silence_chunks = 0
                        speech_chunks = 0
                        speech_buffer = []

                    # 防止单次语音过长
                    elif speech_chunks >= MAX_SPEECH_CHUNKS:
                        logger.warning("语音超过最大时长，强制结束")
                        wav_bytes = build_wav_bytes(speech_buffer)
                        asr_task = asyncio.create_task(recognize_with_funasr(wav_bytes))
                        vp_task = asyncio.create_task(identify_speaker(wav_bytes))
                        text, speaker = await asyncio.gather(asr_task, vp_task)
                        result = {"text": text or "", "speaker": speaker}
                        await websocket.send(json.dumps(result, ensure_ascii=False))
                        # 重置
                        is_speaking = False
                        silence_chunks = 0
                        speech_chunks = 0
                        speech_buffer = []

    except websockets.exceptions.ConnectionClosed:
        logger.info(f"客户端断开: {addr} device={device_id}")
    except Exception as e:
        logger.error(f"处理客户端出错: {e}", exc_info=True)
    finally:
        # 只移除自己的条目（防止覆盖新连接后被误删）
        if _connected_devices.get(device_id) is websocket:
            _connected_devices.pop(device_id)


async def _http_forward_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """极简 asyncio HTTP 服务，支持 POST /api/forward 和 POST /api/suppress"""
    global _suppress_until, _last_tts
    try:
        req_line = (await asyncio.wait_for(reader.readline(), timeout=5)).decode()
        path = req_line.split()[1] if len(req_line.split()) > 1 else "/"
        headers: dict[str, str] = {}
        while True:
            line = (await asyncio.wait_for(reader.readline(), timeout=5)).decode().strip()
            if not line:
                break
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.lower().strip()] = v.strip()

        clen = int(headers.get("content-length", "0"))
        body_bytes = await asyncio.wait_for(reader.read(clen), timeout=5) if clen else b"{}"

        ok = False
        try:
            data = json.loads(body_bytes)

            if path == "/api/suppress":
                # 已弃用：保留接口兼容性，不再实际抑制 VAD
                ok = True

            elif path == "/api/forward":
                device  = data.get("device", "")
                message = data.get("message", {})

                # 缓存 tts_url 消息（供新连接设备补发）
                if message.get("type") == "tts_url" and message.get("url"):
                    _last_tts = {"url": message["url"], "ts": time.time()}

                if device == "*":
                    # 广播给所有已连接设备
                    for dev_id, ws in list(_connected_devices.items()):
                        try:
                            await ws.send(json.dumps(message, ensure_ascii=False))
                            logger.info(f"广播 {message.get('type','?')} → {dev_id}")
                            ok = True
                        except Exception as e:
                            logger.debug(f"广播到 {dev_id} 失败: {e}")
                elif device.endswith("*"):
                    # 前缀匹配，如 "esp32*"
                    prefix = device[:-1]
                    for dev_id, ws in list(_connected_devices.items()):
                        if dev_id.startswith(prefix):
                            try:
                                await ws.send(json.dumps(message, ensure_ascii=False))
                                logger.info(f"前缀匹配转发 {message.get('type','?')} → {dev_id}")
                                ok = True
                            except Exception as e:
                                logger.debug(f"转发到 {dev_id} 失败: {e}")
                else:
                    ws = _connected_devices.get(device)
                    if ws:
                        await ws.send(json.dumps(message, ensure_ascii=False))
                        ok = True
                        logger.info(f"转发 {message.get('type','?')} → {device}")
                    else:
                        logger.debug(f"设备 {device!r} 未连接，已知: {list(_connected_devices.keys())}")
        except Exception as e:
            logger.warning(f"forward handler error: {e}")

        resp_body = b'{"ok":true}' if ok else b'{"ok":false}'
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + b"Content-Length: " + str(len(resp_body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + resp_body
        )
        await writer.drain()
    except Exception as e:
        logger.debug(f"HTTP handler error: {e}")
    finally:
        writer.close()


async def main():
    logger.info("加载 Silero VAD 模型...")
    _model = load_silero_vad()
    logger.info(f"VAD WebSocket 服务启动: ws://{HOST}:{PORT}")
    logger.info(f"VAD HTTP API 启动: http://0.0.0.0:{HTTP_API_PORT}")
    logger.info(f"配置: threshold={VAD_THRESHOLD}, min_silence={MIN_SILENCE_MS}ms")
    logger.info(f"FunASR: {FUNASR_WS_URL}")
    logger.info(f"voiceprint-api: {VOICEPRINT_API_URL}")

    http_server = await asyncio.start_server(
        _http_forward_handler, "0.0.0.0", HTTP_API_PORT
    )

    async with websockets.serve(handle_client, HOST, PORT):
        async with http_server:
            await asyncio.Future()  # 永久运行


if __name__ == "__main__":
    asyncio.run(main())
