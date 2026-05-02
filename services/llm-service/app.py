#!/usr/bin/env python3
"""
LLM Service — 语音对话智能体
接收 ASR 识别结果 + 说话人信息，调用 MiniMax M2.7 生成回复，
并将结果推送到 Dashboard 实时展示。
"""

import json
import logging
import os
from collections import defaultdict, deque

import httpx
import uvicorn
from fastapi import FastAPI
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
MINIMAX_MODEL    = os.getenv("MINIMAX_MODEL", "MiniMax-M2.7")
DASHBOARD_URL    = os.getenv("DASHBOARD_URL", "http://dashboard:8080")

SYSTEM_PROMPT = """你是一个简洁智能的语音助手，以自然的口语风格回答问题。
规则：
- 回答不超过 80 字，适合语音播报
- 使用中文回答
- 如果知道说话人姓名，可以亲切地称呼对方
- 不要用 Markdown 格式，只输出纯文字"""

# 每个说话人保留最近 10 轮对话历史
histories: dict[str, deque] = defaultdict(lambda: deque(maxlen=20))

app = FastAPI(title="LLM Service", version="1.0")


async def push_dashboard(event: dict):
    if not DASHBOARD_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.post(f"{DASHBOARD_URL}/api/vad-event", json=event)
    except Exception as e:
        logger.debug(f"push_dashboard failed: {e}")


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
