#!/usr/bin/env python3
"""
TTS Service - 文字转语音服务（MiniMax 后端）

暴露 OpenAI 兼容的 HTTP 接口，将文字合成语音。
后端使用 MiniMax Speech-02 API，无需本地 GPU。

接口：
  POST /v1/audio/speech   OpenAI 兼容接口
  POST /tts               原生简化接口
  GET  /voices            列出可用音色
  GET  /health            健康检查
"""

import asyncio
import io
import json
import logging
import os
import struct
import wave
from typing import AsyncIterator, Optional

import httpx
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [TTS] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ─────────────────────────── 配置 ───────────────────────────
MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY", "")
# 国际版: https://api.minimax.io | 国内版: https://api.minimaxi.com
MINIMAX_BASE_URL = os.getenv("MINIMAX_BASE_URL", "https://api.minimaxi.com")
# t2a_pro 使用 speech-01/speech-02; t2a_v2 使用 speech-01-turbo/speech-02-hd
MINIMAX_MODEL = os.getenv("MINIMAX_MODEL", "speech-01")
# API 端点: t2a_pro (按次计费) | t2a_v2 (流式，需升级计划)
MINIMAX_ENDPOINT = os.getenv("MINIMAX_ENDPOINT", "t2a_pro")
DEFAULT_VOICE = os.getenv("DEFAULT_VOICE", "female-shaonv")
DEFAULT_SAMPLE_RATE = int(os.getenv("DEFAULT_SAMPLE_RATE", "32000"))
DEFAULT_BITRATE = int(os.getenv("DEFAULT_BITRATE", "128000"))
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8766"))

# MiniMax 内置音色列表（中英文通用）
AVAILABLE_VOICES = {
    # 中文音色
    "female-shaonv": "少女音（中文，活泼）",
    "female-yujie": "御姐音（中文，成熟）",
    "female-chengshu": "成熟女声（中文）",
    "female-tianmei": "甜美女声（中文）",
    "male-qn-qingse": "青涩青年音色（中文）",
    "male-qn-jingying": "精英青年音色（中文）",
    "male-qn-badao": "霸道青年音色（中文）",
    "male-qn-daxuesheng": "青年大学生音色（中文）",
    "presenter_male": "男性主播（中文）",
    "presenter_female": "女性主播（中文）",
    "audiobook_male_1": "男性有声书1（中文）",
    "audiobook_male_2": "男性有声书2（中文）",
    "audiobook_female_1": "女性有声书1（中文）",
    "audiobook_female_2": "女性有声书2（中文）",
    # 英文音色
    "female-en-lilyrose": "Lily Rose（英文，活泼女声）",
    "male-en-Boston": "Boston（英文，男声）",
}


# ─────────────────────────── 数据模型 ───────────────────────────
class OpenAISpeechRequest(BaseModel):
    """兼容 OpenAI /v1/audio/speech 的请求体"""

    model: str = Field(default="tts-1", description="模型名（此处映射到 MiniMax）")
    input: str = Field(..., description="待合成的文字，最多 10000 字")
    voice: str = Field(default=DEFAULT_VOICE, description="音色 ID")
    response_format: str = Field(default="mp3", description="输出格式: mp3/wav/pcm")
    speed: float = Field(default=1.0, ge=0.5, le=2.0, description="语速 0.5~2.0")


class TTSRequest(BaseModel):
    """原生简化接口请求体"""

    text: str = Field(..., description="待合成的文字")
    voice_id: str = Field(default=DEFAULT_VOICE, description="音色 ID")
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    volume: float = Field(default=1.0, ge=0.1, le=10.0)
    pitch: int = Field(default=0, ge=-12, le=12)
    format: str = Field(default="mp3", description="输出格式: mp3/wav/pcm/flac")
    sample_rate: int = Field(default=DEFAULT_SAMPLE_RATE)
    stream: bool = Field(default=False, description="是否流式返回")


# ─────────────────────────── MiniMax 客户端 ───────────────────────────
async def minimax_tts_stream(
    text: str,
    voice_id: str,
    speed: float = 1.0,
    volume: float = 1.0,
    pitch: int = 0,
    fmt: str = "mp3",
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> AsyncIterator[bytes]:
    """调用 MiniMax t2a_v2 流式接口，逐块 yield 音频字节。"""
    if not MINIMAX_API_KEY:
        raise HTTPException(status_code=500, detail="MINIMAX_API_KEY 未配置")

    url = f"{MINIMAX_BASE_URL}/v1/t2a_v2"
    headers = {
        "Authorization": f"Bearer {MINIMAX_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MINIMAX_MODEL,
        "text": text,
        "stream": True,
        "voice_setting": {
            "voice_id": voice_id,
            "speed": speed,
            "vol": volume,
            "pitch": pitch,
        },
        "audio_setting": {
            "audio_sample_rate": sample_rate,
            "bitrate": DEFAULT_BITRATE,
            "format": fmt,
            "channel": 1,
        },
    }

    logger.info("MiniMax TTS(v2 stream): voice=%s fmt=%s len=%d", voice_id, fmt, len(text))

    async with httpx.AsyncClient(timeout=60.0) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                logger.error("MiniMax API error %d: %s", resp.status_code, body[:200])
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=f"MiniMax API 返回错误: {resp.status_code}",
                )

            async for line in resp.aiter_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw == "[DONE]":
                    break
                try:
                    chunk = json.loads(raw)
                    hex_audio = chunk.get("data", {}).get("audio", "")
                    if hex_audio:
                        yield bytes.fromhex(hex_audio)
                except (json.JSONDecodeError, ValueError) as e:
                    logger.warning("解析 SSE chunk 失败: %s | raw=%s", e, raw[:80])


async def minimax_tts_pro(
    text: str,
    voice_id: str,
    speed: float = 1.0,
    volume: float = 1.0,
    pitch: int = 0,
    fmt: str = "mp3",
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> bytes:
    """调用 MiniMax t2a_pro 接口（非流式，返回音频 URL 后下载）。"""
    if not MINIMAX_API_KEY:
        raise HTTPException(status_code=500, detail="MINIMAX_API_KEY 未配置")

    url = f"{MINIMAX_BASE_URL}/v1/t2a_pro"
    headers = {
        "Authorization": f"Bearer {MINIMAX_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MINIMAX_MODEL,
        "text": text,
        "timber_weights": [{"voice_id": voice_id, "weight": 1}],
        "voice_setting": {"speed": speed, "vol": volume, "pitch": pitch},
        "audio_setting": {
            "audio_sample_rate": sample_rate,
            "bitrate": DEFAULT_BITRATE,
            "format": fmt,
        },
    }

    logger.info("MiniMax TTS(pro): voice=%s fmt=%s len=%d", voice_id, fmt, len(text))

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        data = resp.json()
        base = data.get("base_resp", {})
        if base.get("status_code", -1) != 0:
            msg = base.get("status_msg", "unknown error")
            logger.error("MiniMax t2a_pro error: %s", msg)
            raise HTTPException(status_code=502, detail=f"MiniMax API 错误: {msg}")

        audio_url = data.get("audio_file", "")
        if not audio_url:
            raise HTTPException(status_code=502, detail="MiniMax 返回空音频 URL")

        # 下载音频文件
        audio_resp = await client.get(audio_url, timeout=30.0)
        if audio_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="音频文件下载失败")
        return audio_resp.content


async def minimax_tts_full(
    text: str,
    voice_id: str,
    speed: float = 1.0,
    volume: float = 1.0,
    pitch: int = 0,
    fmt: str = "mp3",
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> bytes:
    """根据 MINIMAX_ENDPOINT 选择合适的接口，返回完整音频字节。"""
    if MINIMAX_ENDPOINT == "t2a_v2":
        chunks = []
        async for chunk in minimax_tts_stream(
            text, voice_id, speed, volume, pitch, fmt, sample_rate
        ):
            chunks.append(chunk)
        return b"".join(chunks)
    else:
        return await minimax_tts_pro(text, voice_id, speed, volume, pitch, fmt, sample_rate)


# ─────────────────────────── FastAPI ───────────────────────────
app = FastAPI(
    title="TTS Service",
    description="文字转语音服务（MiniMax Speech-02 后端）",
    version="1.0.0",
)

MIME_TYPES = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "pcm": "audio/pcm",
    "flac": "audio/flac",
}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "backend": "minimax",
        "model": MINIMAX_MODEL,
        "endpoint": MINIMAX_ENDPOINT,
        "base_url": MINIMAX_BASE_URL,
        "api_configured": bool(MINIMAX_API_KEY),
    }


@app.get("/voices")
async def list_voices():
    return {
        "voices": [
            {"voice_id": vid, "description": desc}
            for vid, desc in AVAILABLE_VOICES.items()
        ],
        "default": DEFAULT_VOICE,
    }


@app.post("/v1/audio/speech")
async def openai_speech(req: OpenAISpeechRequest):
    """OpenAI 兼容接口：POST /v1/audio/speech"""
    fmt = req.response_format if req.response_format in MIME_TYPES else "mp3"
    voice = req.voice if req.voice in AVAILABLE_VOICES else DEFAULT_VOICE

    audio = await minimax_tts_full(
        text=req.input,
        voice_id=voice,
        speed=req.speed,
        fmt=fmt,
    )
    if not audio:
        raise HTTPException(status_code=502, detail="MiniMax 返回空音频")

    return Response(
        content=audio,
        media_type=MIME_TYPES.get(fmt, "audio/mpeg"),
        headers={"Content-Disposition": f'attachment; filename="speech.{fmt}"'},
    )


@app.post("/tts")
async def tts_endpoint(req: TTSRequest):
    """原生接口：POST /tts，支持流式和非流式"""
    fmt = req.format if req.format in MIME_TYPES else "mp3"
    voice = req.voice_id if req.voice_id in AVAILABLE_VOICES else DEFAULT_VOICE

    if req.stream:
        if MINIMAX_ENDPOINT != "t2a_v2":
            raise HTTPException(
                status_code=400,
                detail="流式输出仅在 MINIMAX_ENDPOINT=t2a_v2 时支持",
            )

        async def audio_stream():
            async for chunk in minimax_tts_stream(
                text=req.text,
                voice_id=voice,
                speed=req.speed,
                volume=req.volume,
                pitch=req.pitch,
                fmt=fmt,
                sample_rate=req.sample_rate,
            ):
                yield chunk

        return StreamingResponse(
            audio_stream(),
            media_type=MIME_TYPES.get(fmt, "audio/mpeg"),
            headers={"X-Voice-ID": voice, "X-Format": fmt},
        )
    else:
        audio = await minimax_tts_full(
            text=req.text,
            voice_id=voice,
            speed=req.speed,
            volume=req.volume,
            pitch=req.pitch,
            fmt=fmt,
            sample_rate=req.sample_rate,
        )
        if not audio:
            raise HTTPException(status_code=502, detail="MiniMax 返回空音频")
        return Response(
            content=audio,
            media_type=MIME_TYPES.get(fmt, "audio/mpeg"),
            headers={
                "Content-Disposition": f'attachment; filename="tts.{fmt}"',
                "X-Voice-ID": voice,
            },
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
