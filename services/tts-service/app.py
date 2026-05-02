#!/usr/bin/env python3
"""
TTS Service - 文字转语音服务

后端可选：
  - edge    : 微软 Edge 神经网络 TTS（免费，无需 GPU，中文质量好）[默认]
  - minimax : MiniMax Speech API（需账户余额）

接口：
  POST /v1/audio/speech   OpenAI 兼容接口
  POST /tts               原生简化接口
  GET  /voices            列出可用音色
  GET  /health            健康检查
"""

import json
import logging
import os
from typing import AsyncIterator

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
# 后端选择: "edge"（默认，免费）或 "minimax"（需充值）
TTS_BACKEND = os.getenv("TTS_BACKEND", "edge")

# MiniMax 配置
MINIMAX_API_KEY    = os.getenv("MINIMAX_API_KEY", "")
MINIMAX_BASE_URL   = os.getenv("MINIMAX_BASE_URL", "https://api.minimaxi.com")
MINIMAX_MODEL      = os.getenv("MINIMAX_MODEL", "speech-01")
MINIMAX_ENDPOINT   = os.getenv("MINIMAX_ENDPOINT", "t2a_pro")

# 通用配置
DEFAULT_VOICE      = os.getenv("DEFAULT_VOICE", "zh-CN-XiaoxiaoNeural")  # edge 默认
DEFAULT_SAMPLE_RATE = int(os.getenv("DEFAULT_SAMPLE_RATE", "32000"))
DEFAULT_BITRATE    = int(os.getenv("DEFAULT_BITRATE", "128000"))
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8766"))

# ─────────────────────────── 音色表 ───────────────────────────
EDGE_VOICES = {
    "zh-CN-XiaoxiaoNeural":  "晓晓（中文女声，活泼）",
    "zh-CN-XiaoyiNeural":    "晓伊（中文女声，温柔）",
    "zh-CN-YunjianNeural":   "云健（中文男声，激昂）",
    "zh-CN-YunxiNeural":     "云希（中文男声，清晰）",
    "zh-CN-YunyangNeural":   "云扬（中文男声，主播）",
    "zh-CN-liaoning-XiaobeiNeural": "晓北（东北方言）",
    "zh-TW-HsiaoChenNeural": "曉臻（台湾中文）",
    "en-US-JennyNeural":     "Jenny（英文女声）",
    "en-US-GuyNeural":       "Guy（英文男声）",
    "en-GB-SoniaNeural":     "Sonia（英式英文）",
}

MINIMAX_VOICES = {
    "female-shaonv":        "少女音（中文，活泼）",
    "female-yujie":         "御姐音（中文，成熟）",
    "female-chengshu":      "成熟女声（中文）",
    "female-tianmei":       "甜美女声（中文）",
    "male-qn-qingse":       "青涩青年（中文）",
    "male-qn-jingying":     "精英青年（中文）",
    "male-qn-badao":        "霸道青年（中文）",
    "presenter_male":       "男性主播（中文）",
    "presenter_female":     "女性主播（中文）",
    "audiobook_male_1":     "有声书男声1",
    "audiobook_female_1":   "有声书女声1",
    "female-en-lilyrose":   "Lily Rose（英文）",
    "male-en-Boston":       "Boston（英文）",
}


# ─────────────────────────── 数据模型 ───────────────────────────
class OpenAISpeechRequest(BaseModel):
    model: str = Field(default="tts-1")
    input: str = Field(..., description="待合成文字，最多 10000 字")
    voice: str = Field(default=DEFAULT_VOICE)
    response_format: str = Field(default="mp3")
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


class TTSRequest(BaseModel):
    text: str = Field(...)
    voice_id: str = Field(default=DEFAULT_VOICE)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    volume: float = Field(default=1.0, ge=0.1, le=10.0)
    pitch: int = Field(default=0, ge=-12, le=12)
    format: str = Field(default="mp3")
    sample_rate: int = Field(default=DEFAULT_SAMPLE_RATE)
    stream: bool = Field(default=False)


MIME_TYPES = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "pcm": "audio/pcm",
    "flac": "audio/flac",
}


# ─────────────────────────── Edge TTS 后端 ───────────────────────────
async def edge_tts_synthesize(
    text: str,
    voice: str = "zh-CN-XiaoxiaoNeural",
    rate: str = "+0%",
    volume: str = "+0%",
) -> bytes:
    """调用 edge-tts 合成，返回 MP3 字节。"""
    try:
        import edge_tts  # type: ignore
    except ImportError:
        raise HTTPException(status_code=500, detail="edge-tts 未安装")

    communicate = edge_tts.Communicate(text, voice, rate=rate, volume=volume)
    audio_chunks = []
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_chunks.append(chunk["data"])
    if not audio_chunks:
        raise HTTPException(status_code=502, detail="Edge TTS 返回空音频")
    return b"".join(audio_chunks)


async def edge_tts_stream(
    text: str,
    voice: str = "zh-CN-XiaoxiaoNeural",
    rate: str = "+0%",
) -> AsyncIterator[bytes]:
    """Edge TTS 流式 yield。"""
    try:
        import edge_tts  # type: ignore
    except ImportError:
        raise HTTPException(status_code=500, detail="edge-tts 未安装")

    communicate = edge_tts.Communicate(text, voice, rate=rate)
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            yield chunk["data"]


def _speed_to_edge_rate(speed: float) -> str:
    """将 0.5~2.0 速度转换为 Edge TTS rate 格式（如 +20%）。"""
    pct = int((speed - 1.0) * 100)
    return f"+{pct}%" if pct >= 0 else f"{pct}%"


# ─────────────────────────── MiniMax 后端 ───────────────────────────
async def minimax_tts_pro(
    text: str,
    voice_id: str,
    speed: float = 1.0,
    volume: float = 1.0,
    pitch: int = 0,
    fmt: str = "mp3",
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> bytes:
    """MiniMax t2a_pro（非流式，返回音频 URL 后下载）。"""
    if not MINIMAX_API_KEY:
        raise HTTPException(status_code=500, detail="MINIMAX_API_KEY 未配置")

    url = f"{MINIMAX_BASE_URL}/v1/t2a_pro"
    headers = {"Authorization": f"Bearer {MINIMAX_API_KEY}", "Content-Type": "application/json"}
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
            raise HTTPException(status_code=502, detail=f"MiniMax API 错误: {base.get('status_msg')}")
        audio_url = data.get("audio_file", "")
        if not audio_url:
            raise HTTPException(status_code=502, detail="MiniMax 返回空音频 URL")
        audio_resp = await client.get(audio_url, timeout=30.0)
        return audio_resp.content


async def minimax_tts_v2_stream(
    text: str,
    voice_id: str,
    speed: float = 1.0,
    volume: float = 1.0,
    pitch: int = 0,
    fmt: str = "mp3",
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> AsyncIterator[bytes]:
    """MiniMax t2a_v2 流式（需升级计划）。"""
    if not MINIMAX_API_KEY:
        raise HTTPException(status_code=500, detail="MINIMAX_API_KEY 未配置")

    url = f"{MINIMAX_BASE_URL}/v1/t2a_v2"
    headers = {"Authorization": f"Bearer {MINIMAX_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": MINIMAX_MODEL,
        "text": text,
        "stream": True,
        "voice_setting": {"voice_id": voice_id, "speed": speed, "vol": volume, "pitch": pitch},
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
                raise HTTPException(status_code=resp.status_code, detail=f"MiniMax API error: {body[:200]}")
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
                    logger.warning("解析 SSE chunk 失败: %s", e)


async def minimax_synthesize(text: str, voice_id: str, speed: float, volume: float,
                              pitch: int, fmt: str, sample_rate: int) -> bytes:
    if MINIMAX_ENDPOINT == "t2a_v2":
        chunks = []
        async for c in minimax_tts_v2_stream(text, voice_id, speed, volume, pitch, fmt, sample_rate):
            chunks.append(c)
        return b"".join(chunks)
    return await minimax_tts_pro(text, voice_id, speed, volume, pitch, fmt, sample_rate)


# ─────────────────────────── 统一合成入口 ───────────────────────────
async def synthesize(
    text: str,
    voice_id: str,
    speed: float = 1.0,
    volume: float = 1.0,
    pitch: int = 0,
    fmt: str = "mp3",
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> bytes:
    if TTS_BACKEND == "minimax":
        return await minimax_synthesize(text, voice_id, speed, volume, pitch, fmt, sample_rate)
    # edge backend（默认）
    rate = _speed_to_edge_rate(speed)
    return await edge_tts_synthesize(text, voice_id, rate=rate)


# ─────────────────────────── FastAPI ───────────────────────────
app = FastAPI(
    title="TTS Service",
    description="文字转语音服务（edge-tts 免费后端 / MiniMax 高质量后端）",
    version="2.0.0",
)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "backend": TTS_BACKEND,
        "model": MINIMAX_MODEL if TTS_BACKEND == "minimax" else "edge-neural",
        "endpoint": MINIMAX_ENDPOINT if TTS_BACKEND == "minimax" else "edge-tts",
        "default_voice": DEFAULT_VOICE,
        "minimax_api_configured": bool(MINIMAX_API_KEY),
    }


@app.get("/voices")
async def list_voices():
    if TTS_BACKEND == "minimax":
        voices = [{"voice_id": k, "description": v, "backend": "minimax"}
                  for k, v in MINIMAX_VOICES.items()]
    else:
        voices = [{"voice_id": k, "description": v, "backend": "edge"}
                  for k, v in EDGE_VOICES.items()]
    # 总是追加另一个后端的音色，标注需切换后端
    other_backend = "minimax" if TTS_BACKEND == "edge" else "edge"
    other_voices = MINIMAX_VOICES if other_backend == "minimax" else EDGE_VOICES
    voices += [{"voice_id": k, "description": v, "backend": other_backend, "note": f"需设置 TTS_BACKEND={other_backend}"}
               for k, v in other_voices.items()]
    return {"active_backend": TTS_BACKEND, "default_voice": DEFAULT_VOICE, "voices": voices}


@app.post("/v1/audio/speech")
async def openai_speech(req: OpenAISpeechRequest):
    """OpenAI 兼容接口：POST /v1/audio/speech"""
    fmt = req.response_format if req.response_format in MIME_TYPES else "mp3"
    audio = await synthesize(
        text=req.input, voice_id=req.voice, speed=req.speed, fmt=fmt,
    )
    return Response(
        content=audio,
        media_type=MIME_TYPES.get(fmt, "audio/mpeg"),
        headers={"Content-Disposition": f'attachment; filename="speech.{fmt}"'},
    )


@app.post("/tts")
async def tts_endpoint(req: TTSRequest):
    """原生接口：POST /tts，支持流式（edge 后端）和非流式"""
    fmt = req.format if req.format in MIME_TYPES else "mp3"

    if req.stream and TTS_BACKEND == "edge":
        rate = _speed_to_edge_rate(req.speed)
        return StreamingResponse(
            edge_tts_stream(req.text, req.voice_id, rate=rate),
            media_type="audio/mpeg",
            headers={"X-Voice-ID": req.voice_id, "X-Backend": "edge"},
        )

    audio = await synthesize(
        text=req.text, voice_id=req.voice_id, speed=req.speed,
        volume=req.volume, pitch=req.pitch, fmt=fmt, sample_rate=req.sample_rate,
    )
    return Response(
        content=audio,
        media_type=MIME_TYPES.get(fmt, "audio/mpeg"),
        headers={
            "Content-Disposition": f'attachment; filename="tts.{fmt}"',
            "X-Voice-ID": req.voice_id,
            "X-Backend": TTS_BACKEND,
        },
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
