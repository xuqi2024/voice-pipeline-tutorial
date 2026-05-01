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

MIN_SILENCE_CHUNKS = int(MIN_SILENCE_MS / (CHUNK_SAMPLES / SAMPLE_RATE * 1000))
MIN_SPEECH_CHUNKS = int(MIN_SPEECH_MS / (CHUNK_SAMPLES / SAMPLE_RATE * 1000))
MAX_SPEECH_CHUNKS = int(MAX_SPEECH_MS / (CHUNK_SAMPLES / SAMPLE_RATE * 1000))


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
    logger.info(f"客户端连接: {addr}")

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
                        logger.info(f"▶ 检测到语音开始 (prob={prob:.2f})")
                        asyncio.create_task(push_event({"type": "speech_start", "prob": round(prob, 3)}))
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
                            logger.info(f"■ 语音结束 ({duration_ms:.0f}ms, {len(speech_buffer)} 块)")
                            asyncio.create_task(push_event({"type": "speech_end", "duration_ms": round(duration_ms)}))

                            wav_bytes = build_wav_bytes(speech_buffer)

                            # 并行调用 ASR 和声纹识别
                            asr_task = asyncio.create_task(recognize_with_funasr(wav_bytes))
                            vp_task = asyncio.create_task(identify_speaker(wav_bytes))
                            text, speaker = await asyncio.gather(asr_task, vp_task)

                            # 推送各自结果到 Dashboard
                            asyncio.create_task(push_event({"type": "asr_result", "text": text or ""}))
                            asyncio.create_task(push_event({
                                "type": "voiceprint_result",
                                "speaker": speaker["id"] if speaker else None,
                                "score": speaker["score"] if speaker else None,
                            }))

                            result = {
                                "text": text or "",
                                "speaker": speaker,
                            }
                            logger.info(f"结果: text={text!r} speaker={speaker}")
                            asyncio.create_task(push_event({
                                "type": "result",
                                "text": text or "",
                                "speaker": speaker["id"] if speaker else None,
                            }))
                            await websocket.send(json.dumps(result, ensure_ascii=False))
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
        logger.info(f"客户端断开: {addr}")
    except Exception as e:
        logger.error(f"处理客户端出错: {e}", exc_info=True)


async def main():
    logger.info(f"加载 Silero VAD 模型...")
    # 预热模型（验证可以加载）
    _model = load_silero_vad()
    logger.info(f"VAD 服务启动: ws://{HOST}:{PORT}")
    logger.info(f"配置: threshold={VAD_THRESHOLD}, min_silence={MIN_SILENCE_MS}ms")
    logger.info(f"FunASR: {FUNASR_WS_URL}")
    logger.info(f"voiceprint-api: {VOICEPRINT_API_URL}")

    async with websockets.serve(handle_client, HOST, PORT):
        await asyncio.Future()  # 永久运行


if __name__ == "__main__":
    asyncio.run(main())
