#!/usr/bin/env python3
"""
Voice Pipeline Dashboard
提供 Web UI 展示实时语音处理流程，支持声纹注册管理
"""

import asyncio
import json
import logging
import os
import time
from typing import List

import httpx
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [DASH] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ──────────────────────────── 配置 ────────────────────────────
PORT = int(os.getenv("PORT", "8080"))
VOICEPRINT_URL = os.getenv("VOICEPRINT_API_URL", "http://voiceprint-api:8005")
API_KEY = os.getenv("VOICEPRINT_API_KEY", "de395e06-035c-44f9-9a6b-8ef126a8bea0")

app = FastAPI(title="Voice Pipeline Dashboard", docs_url="/api/docs")


# ──────────────────────────── WebSocket 广播 ──────────────────
class BroadcastManager:
    def __init__(self):
        self.connections: List[WebSocket] = []
        self.history: List[dict] = []  # 保留最近 200 条事件

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.connections.append(ws)
        # 将历史事件发给新连接的浏览器
        for event in self.history[-100:]:
            try:
                await ws.send_json(event)
            except Exception:
                pass
        logger.info(f"浏览器连接 (共 {len(self.connections)} 个)")

    def disconnect(self, ws: WebSocket):
        if ws in self.connections:
            self.connections.remove(ws)
        logger.info(f"浏览器断开 (剩余 {len(self.connections)} 个)")

    async def broadcast(self, event: dict):
        if "ts" not in event:
            event["ts"] = time.time()
        self.history.append(event)
        if len(self.history) > 200:
            self.history = self.history[-100:]

        dead = []
        for ws in list(self.connections):
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = BroadcastManager()


# ──────────────────────────── VAD 事件接收 ────────────────────
@app.post("/api/vad-event")
async def receive_vad_event(event: dict):
    """接收 VAD 服务推送的管道事件"""
    await manager.broadcast(event)
    return {"ok": True}


# ──────────────────────────── 浏览器 WebSocket ────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            # 接收心跳保活
            await asyncio.wait_for(ws.receive_text(), timeout=60)
    except (WebSocketDisconnect, asyncio.TimeoutError, Exception):
        manager.disconnect(ws)


# ──────────────────────────── 声纹 API 代理 ───────────────────
def _vp_headers():
    return {"Authorization": f"Bearer {API_KEY}"}


@app.get("/api/speakers/health")
async def speakers_health():
    """声纹服务健康状态（包含已注册数量）"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{VOICEPRINT_URL}/voiceprint/health",
                params={"key": API_KEY},
            )
        return resp.json()
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.post("/api/speakers/register")
async def register_speaker(
    speaker_id: str = Form(..., description="说话人ID/姓名"),
    file: UploadFile = File(..., description="WAV 音频文件 (16kHz mono 推荐)"),
):
    """注册新声纹"""
    audio_bytes = await file.read()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{VOICEPRINT_URL}/voiceprint/register",
            headers=_vp_headers(),
            files={"file": (file.filename or "audio.wav", audio_bytes, "audio/wav")},
            data={"speaker_id": speaker_id},
        )
    result = resp.json()
    if resp.status_code == 200:
        await manager.broadcast({
            "type": "speaker_registered",
            "speaker_id": speaker_id,
            "msg": result.get("msg", ""),
        })
        logger.info(f"注册声纹: {speaker_id}")
    return JSONResponse(result, status_code=resp.status_code)


@app.delete("/api/speakers/{speaker_id}")
async def delete_speaker(speaker_id: str):
    """删除声纹"""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.delete(
            f"{VOICEPRINT_URL}/voiceprint/{speaker_id}",
            headers=_vp_headers(),
        )
    result = resp.json()
    if resp.status_code == 200:
        await manager.broadcast({
            "type": "speaker_deleted",
            "speaker_id": speaker_id,
        })
        logger.info(f"删除声纹: {speaker_id}")
    return JSONResponse(result, status_code=resp.status_code)


# ──────────────────────────── 系统状态 ───────────────────────
@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "browser_connections": len(manager.connections),
        "event_history": len(manager.history),
    }


# ──────────────────────────── 静态文件 ───────────────────────
app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
