#!/usr/bin/env python3
"""
LLM Service — 语音对话智能体
接收 ASR 识别结果 + 说话人信息，调用 MiniMax M3 生成回复，
并将结果推送到 Dashboard 实时展示。
"""

import base64
import json
import logging
import os
import uuid
from collections import defaultdict, deque

import httpx
import uvicorn
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [LLM] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ──────────────────────────── 配置 ────────────────────────────
PORT = int(os.getenv("PORT", "8006"))
MINIMAX_API_KEY  = os.getenv("MINIMAX_API_KEY", "")
MINIMAX_BASE_URL = os.getenv("MINIMAX_BASE_URL",
                             "https://api.minimaxi.com/anthropic/v1/messages")
MINIMAX_MODEL    = os.getenv("MINIMAX_MODEL", "MiniMax-M3")
DASHBOARD_URL    = os.getenv("DASHBOARD_URL", "http://dashboard:8080")
TTS_SERVICE_URL  = os.getenv("TTS_SERVICE_URL", "http://172.18.0.1:8766")
TTS_VOICE_ID     = os.getenv("TTS_VOICE_ID", "female-shaonv")
VAD_SERVICE_URL  = os.getenv("VAD_SERVICE_URL", "http://vad-service:8767")
HOST_IP          = os.getenv("HOST_IP", "192.168.1.8")

SYSTEM_PROMPT = """你是一个简洁智能的语音助手，以自然的口语风格回答问题。
规则：
- 回答不超过 80 字，适合语音播报
- 使用中文回答
- 如果知道说话人姓名，可以亲切地称呼对方
- 不要用 Markdown 格式，只输出纯文字"""

# 每个说话人保留最近 10 轮对话历史
histories: dict[str, deque] = defaultdict(lambda: deque(maxlen=20))

# 音频缓存：audio_id → WAV bytes（最多保留 20 条）
_audio_cache: dict[str, bytes] = {}
_AUDIO_CACHE_MAX = 20

app = FastAPI(title="LLM Service", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)


async def push_dashboard(event: dict):
    if not DASHBOARD_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.post(f"{DASHBOARD_URL}/api/vad-event", json=event)
    except Exception as e:
        logger.debug(f"push_dashboard failed: {e}")


def _find_wav_data_offset(wav_bytes: bytes) -> int:
    """Find the offset where PCM data starts in a WAV file (handles LIST chunks)."""
    i = 12
    while i < min(len(wav_bytes) - 8, 512):
        chunk_id = wav_bytes[i:i+4]
        if chunk_id == b'data':
            return i + 8
        chunk_size = int.from_bytes(wav_bytes[i+4:i+8], 'little')
        i += 8 + (chunk_size + 1 & ~1)  # pad to even
    return 44  # fallback


async def call_tts(text: str) -> tuple[str, bytes] | None:
    """调用 TTS 服务生成语音，返回 (audio_id, wav_bytes)。遇到限流时重试 3 次。"""
    import asyncio as _asyncio
    clean_text = text.strip()
    if not TTS_SERVICE_URL or not clean_text:
        return None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                resp = await client.post(
                    f"{TTS_SERVICE_URL}/tts",
                    json={"text": clean_text, "voice_id": TTS_VOICE_ID, "format": "wav"},
                )
                if resp.status_code == 502:
                    body = resp.text
                    if "limit" in body.lower() and attempt < 2:
                        logger.warning(f"TTS 限流 (attempt {attempt+1}), 等待 3s 重试...")
                        await _asyncio.sleep(3)
                        continue
                resp.raise_for_status()
                wav_bytes = resp.content
            audio_id = uuid.uuid4().hex[:12]
            if len(_audio_cache) >= _AUDIO_CACHE_MAX:
                del _audio_cache[next(iter(_audio_cache))]
            _audio_cache[audio_id] = wav_bytes
            logger.info(f"TTS: {len(wav_bytes)} bytes cached as {audio_id}")
            return audio_id, wav_bytes
        except Exception as e:
            logger.warning(f"TTS call failed (attempt {attempt+1}): {e}")
            if attempt < 2:
                await _asyncio.sleep(2)
    return None


async def forward_to_device(device: str, message: dict):
    """通过 VAD 服务将消息发送到指定设备的 WebSocket"""
    if not VAD_SERVICE_URL or not device:
        return
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post(
                f"{VAD_SERVICE_URL}/api/forward",
                json={"device": device, "message": message},
            )
    except Exception as e:
        logger.debug(f"forward_to_device({device}) failed: {e}")


@app.get("/api/audio/{audio_id}")
async def get_audio(audio_id: str):
    """提供缓存的 TTS 音频文件"""
    if audio_id not in _audio_cache:
        return JSONResponse({"error": "not found"}, status_code=404)
    return Response(content=_audio_cache[audio_id], media_type="audio/wav",
                    headers={"Cache-Control": "no-cache"})


@app.post("/api/chat")
async def chat(body: dict):
    """接收语音识别结果并生成 LLM 回复"""
    text    = (body.get("text") or "").strip()
    speaker = body.get("speaker") or "unknown"
    device  = body.get("device") or ""

    if not text:
        return JSONResponse({"error": "no text"}, status_code=400)

    logger.info(f"▶ [{device}] speaker={speaker}: {text!r}")
    await push_dashboard({"type": "llm_thinking", "text": text,
                          "speaker": speaker, "device": device})

    history = histories[speaker]
    messages = list(history) + [{"role": "user", "content": text}]

    system = SYSTEM_PROMPT
    if speaker and speaker != "unknown":
        system += f"\n\n当前说话人：{speaker}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                MINIMAX_BASE_URL,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": MINIMAX_API_KEY,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": MINIMAX_MODEL,
                    "system": system,
                    "messages": messages,
                    "max_tokens": 300,
                },
            )
        data = resp.json()
    except Exception as e:
        logger.error(f"MiniMax API error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

    # 提取回复文本（Anthropic 格式：content[].type=text）
    reply = "".join(
        b.get("text", "") for b in data.get("content", [])
        if b.get("type") == "text"
    )
    if not reply:
        logger.warning(f"LLM empty reply: {data}")
        return JSONResponse({"error": "empty reply"}, status_code=500)

    logger.info(f"◀ [{device}] {reply!r}")

    # 更新对话历史
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply})

    # 推送到 Dashboard
    await push_dashboard({
        "type": "llm_response",
        "text": text,
        "reply": reply,
        "speaker": speaker,
        "device": device,
    })

    # 调用 TTS 生成语音
    import asyncio
    tts_result = await call_tts(reply)
    if tts_result:
        audio_id, wav_bytes = tts_result
        audio_url = f"http://{HOST_IP}:8006/api/audio/{audio_id}"

        # 计算 TTS 时长（32kHz 16-bit mono），仅供日志参考
        pcm_start = _find_wav_data_offset(wav_bytes)
        pcm_bytes  = len(wav_bytes) - pcm_start
        tts_duration = pcm_bytes / (32000 * 2)  # seconds

        # 推送 TTS 就绪事件到 Dashboard（含音频 URL，浏览器直接播放）
        await push_dashboard({
            "type": "tts_ready",
            "reply": reply,
            "speaker": speaker,
            "device": device,
            "audio_url": audio_url,
            "audio_id": audio_id,
        })

        logger.info(f"TTS 时长 {tts_duration:.1f}s，广播给所有设备")

        # 广播给所有已连接设备，包含 TTS 文本供 VAD 做回声检测
        asyncio.create_task(forward_to_device("*", {
            "type": "tts_url",
            "url": audio_url,
            "text": reply,            # 用于文本相似度回声检测
            "duration": tts_duration, # 用于时间窗口判断
        }))

    return {"reply": reply, "speaker": speaker, "device": device}


@app.delete("/api/history/{speaker_id}")
async def clear_history(speaker_id: str):
    """清除指定说话人的对话历史"""
    if speaker_id in histories:
        histories[speaker_id].clear()
    return {"ok": True, "speaker": speaker_id}


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "model": MINIMAX_MODEL,
        "speakers_with_history": len(histories),
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
