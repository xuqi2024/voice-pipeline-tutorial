#!/usr/bin/env python3
"""
Persona Agent - 人物画像旁听系统

角色：旁听者，不主动回复。
功能：
  - 收集所有说话人的对话片段（VAD 转发）
  - 批量送给 LLM 分析：① 是否对AI说话 ② 人物画像提取
  - 短期记忆（当次会话） + 长期记忆（持久化画像）
  - REST API 供 dashboard 展示
"""

import asyncio, base64, hashlib, json, logging, os, sqlite3, uuid
from datetime import datetime, timedelta
from typing import Optional, List
import httpx
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [PERSONA] %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

# ── 配置 ──────────────────────────────────────────────────────────────────────
MINIMAX_API_KEY  = os.getenv("MINIMAX_API_KEY", "")
MINIMAX_BASE_URL = os.getenv("MINIMAX_BASE_URL", "https://api.minimaxi.com/anthropic/v1/messages")
MINIMAX_MODEL    = os.getenv("MINIMAX_MODEL", "MiniMax-M2.7")
VOICEPRINT_API_URL = os.getenv("VOICEPRINT_API_URL", "http://voiceprint-api:8005")
VOICEPRINT_API_KEY = os.getenv("VOICEPRINT_API_KEY", "")
DB_PATH = os.getenv("DB_PATH", "/app/data/persona.db")

# 批量分析触发条件
BATCH_MIN_UTTERANCES = int(os.getenv("BATCH_MIN_UTTERANCES", "8"))   # 积累 N 条触发
BATCH_MAX_WAIT_SEC   = int(os.getenv("BATCH_MAX_WAIT_SEC", "300"))   # 或 5 分钟强制触发
BATCH_DEBOUNCE_SEC   = int(os.getenv("BATCH_DEBOUNCE_SEC", "120"))   # 同一说话人最快 2 分钟分析一次
STM_TTL_MINUTES      = int(os.getenv("STM_TTL_MINUTES", "60"))
UNKNOWN_VP_THRESHOLD = float(os.getenv("UNKNOWN_VP_THRESHOLD", "0.40"))
UNKNOWN_MIN_DURATION = int(os.getenv("UNKNOWN_MIN_DURATION", "800"))  # ms

app = FastAPI(title="Persona Agent")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── 数据库 ────────────────────────────────────────────────────────────────────
def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS speakers (
            id          TEXT PRIMARY KEY,
            display_name TEXT,
            is_registered INTEGER DEFAULT 0,  -- 1=声纹注册, 0=未知
            first_seen  TEXT DEFAULT (datetime('now')),
            last_seen   TEXT DEFAULT (datetime('now')),
            total_utterances INTEGER DEFAULT 0,
            profile_summary TEXT,
            last_analyzed_at TEXT,
            pending_utterances INTEGER DEFAULT 0,
            avatar_emoji TEXT DEFAULT '👤'
        );
        CREATE TABLE IF NOT EXISTS transcripts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            utterance_id TEXT UNIQUE,
            speaker_id  TEXT NOT NULL,
            text        TEXT NOT NULL,
            ts          TEXT DEFAULT (datetime('now')),
            conv_id     TEXT,
            duration_ms INTEGER,
            directed_at_ai INTEGER DEFAULT -1, -- -1=未分类, 0=人与人, 1=对AI说
            is_filtered INTEGER DEFAULT 0,
            filter_reason TEXT
        );
        CREATE TABLE IF NOT EXISTS profile_facts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            speaker_id  TEXT NOT NULL,
            category    TEXT NOT NULL,
            fact        TEXT NOT NULL,
            evidence    TEXT,
            confidence  REAL DEFAULT 0.7,
            fact_hash   TEXT UNIQUE,
            is_active   INTEGER DEFAULT 1,
            created_at  TEXT DEFAULT (datetime('now')),
            updated_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS short_term_memory (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            speaker_id  TEXT NOT NULL,
            conv_id     TEXT,
            content     TEXT NOT NULL,
            ts          TEXT DEFAULT (datetime('now')),
            expires_at  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_tr_speaker ON transcripts(speaker_id);
        CREATE INDEX IF NOT EXISTS idx_tr_conv    ON transcripts(conv_id);
        CREATE INDEX IF NOT EXISTS idx_pf_speaker ON profile_facts(speaker_id);
        """)
    logger.info("DB ready: %s", DB_PATH)

# ── 模型 ──────────────────────────────────────────────────────────────────────
class SegmentIn(BaseModel):
    utterance_id: Optional[str] = None
    speaker_id:   Optional[str] = None   # None = 未识别
    text:         str
    ts:           Optional[str] = None
    conv_id:      Optional[str] = None
    duration_ms:  Optional[int] = None
    audio_b64:    Optional[str] = None   # 未知说话人时提供，用于声纹注册

class FilterReq(BaseModel):
    reason: Optional[str] = "test_data"

# ── 声纹注册（未知说话人） ─────────────────────────────────────────────────────
_enroll_lock = asyncio.Lock()

async def enroll_unknown(audio_b64: str) -> Optional[str]:
    """识别或注册未知说话人，返回 speaker_id。"""
    if not VOICEPRINT_API_KEY or not audio_b64:
        return None
    try:
        audio = base64.b64decode(audio_b64)
    except Exception:
        return None

    async with _enroll_lock:
        # 先尝试识别（含已注册的未知-* 说话人）
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(f"{VOICEPRINT_API_URL}/voiceprint/identify",
                    headers={"Authorization": VOICEPRINT_API_KEY},
                    files={"file": ("a.wav", audio, "audio/wav")})
            if r.status_code == 200:
                d = r.json()
                sid, score = d.get("speaker_id"), d.get("score", 0)
                if sid and score >= UNKNOWN_VP_THRESHOLD:
                    logger.info(f"未知声纹已匹配: {sid} score={score:.3f}")
                    return sid
        except Exception as e:
            logger.warning(f"声纹识别失败: {e}")

        # 注册为新未知说话人
        with db() as c:
            row = c.execute(
                "SELECT MAX(CAST(SUBSTR(id,4) AS INTEGER)) FROM speakers WHERE id LIKE '未知-%'"
            ).fetchone()
            n = (row[0] or 0) + 1
            new_id = f"未知-{n:03d}"
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(f"{VOICEPRINT_API_URL}/voiceprint/register",
                    headers={"Authorization": VOICEPRINT_API_KEY},
                    data={"speaker_id": new_id},
                    files={"file": ("a.wav", audio, "audio/wav")})
            if r.status_code in (200, 201):
                with db() as c:
                    c.execute("INSERT OR IGNORE INTO speakers(id,display_name,is_registered,avatar_emoji) VALUES(?,?,0,'❓')",
                              (new_id, new_id))
                    c.commit()
                logger.info(f"注册新未知说话人: {new_id}")
                return new_id
        except Exception as e:
            logger.warning(f"声纹注册失败: {e}")
    return None

# ── LLM 批量分析 ───────────────────────────────────────────────────────────────
ANALYSIS_SYSTEM = """你是一个专业的对话分析师和人物画像专家。
你的工作是旁听对话，分析说话人的特征，不参与对话。"""

ANALYSIS_USER_TMPL = """请分析以下对话片段，完成两项任务：

**对话记录：**
{transcript}

**任务1：意图分类**
判断每条发言是"对AI助手说话"还是"人与人之间的对话"。
"对AI说话"的标志：直接提问AI、发指令、测试功能等。
"人与人"的标志：闲聊、讨论、叙述经历、表达观点等。

**任务2：人物画像提取**
仅从"人与人之间的对话"中提取以下维度的信息（不要猜测，只提取有明确依据的）：
- personality（性格特点）
- interest（兴趣爱好）
- opinion（观点立场）
- habit（行为习惯）
- context（背景信息，如职业、生活状态等）

**返回格式（只返回JSON）：**
{{
  "intents": [
    {{"id": 1, "directed_at_ai": false}},
    {{"id": 2, "directed_at_ai": true}}
  ],
  "profiles": {{
    "说话人ID": {{
      "personality": [],
      "interest": [],
      "opinion": [],
      "habit": [],
      "context": []
    }}
  }}
}}"""

SUMMARY_TMPL = """根据以下说话人的画像特征和近期发言，用200字以内写一段中文简介，描述此人的性格特征、兴趣爱好、观点立场、行为习惯等，以及他主要谈论的话题和内容（直接写内容，不要标题）：

说话人：{speaker_id}

【画像特征】
{facts}

【近期发言摘要】
{transcripts}"""

async def call_llm(messages: list, system: str = "") -> Optional[str]:
    """调用 MiniMax Anthropic API."""
    try:
        payload: dict = {"model": MINIMAX_MODEL, "max_tokens": 6000, "messages": messages}
        if system:
            payload["system"] = system
        async with httpx.AsyncClient(timeout=90) as c:
            r = await c.post(MINIMAX_BASE_URL,
                headers={"x-api-key": MINIMAX_API_KEY, "anthropic-version": "2023-06-01"},
                json=payload)
        if r.status_code == 200:
            # MiniMax M2.7 返回 thinking + text 两个 content block，取第一个 type=text 的
            content_blocks = r.json().get("content", [])
            text_block = next((b for b in content_blocks if b.get("type") == "text"), None)
            if text_block:
                return text_block["text"]
            logger.warning(f"LLM 响应无 text block，content types: {[b.get('type') for b in content_blocks]}")
            return None
        logger.warning(f"LLM 调用失败: {r.status_code} {r.text[:200]}")
    except Exception as e:
        logger.warning(f"LLM 异常: {e}")
    return None

async def run_batch_analysis(speaker_id: str):
    """对一个说话人的积累内容做批量分析。"""
    lock = _get_analysis_lock(speaker_id)
    if lock.locked():
        logger.info(f"跳过重复分析请求: {speaker_id}")
        return
    async with lock:
        await _do_batch_analysis(speaker_id)

async def _do_batch_analysis(speaker_id: str):
    with db() as c:
        # 取最近 20 条未过滤的发言（减小 prompt 体积，避免超 token 限制）
        rows = c.execute("""
            SELECT id, text FROM transcripts
            WHERE speaker_id = ? AND is_filtered = 0
            ORDER BY ts DESC LIMIT 20
        """, (speaker_id,)).fetchall()
    if not rows:
        return

    # 构造对话文本（最新在下面）
    lines = "\n".join([f"[{r['id']}] {speaker_id}: {r['text']}" for r in reversed(rows)])
    prompt = ANALYSIS_USER_TMPL.format(transcript=lines)
    result_text = await call_llm([{"role": "user", "content": prompt}], system=ANALYSIS_SYSTEM)
    if not result_text:
        # 即使 LLM 失败也要重置 pending，避免卡死
        with db() as c:
            c.execute("UPDATE speakers SET last_analyzed_at=datetime('now'), pending_utterances=0 WHERE id=?",
                      (speaker_id,))
            c.commit()
        return

    # 解析 JSON（兼容 markdown 代码块，容忍尾部截断）
    try:
        clean = result_text.strip()
        # 去掉 markdown 代码块
        if "```" in clean:
            parts = clean.split("```")
            for part in parts:
                candidate = part.lstrip("json").strip()
                if candidate.startswith("{"):
                    clean = candidate
                    break
        # 尝试完整解析
        try:
            data = json.loads(clean)
        except json.JSONDecodeError:
            # 截断恢复：找到最后一个完整的 intents 项或 profiles 闭合
            last_brace = clean.rfind('}')
            if last_brace > 0:
                # 找到最外层 json 能解析的最大前缀
                for end in range(len(clean), last_brace, -1):
                    try:
                        data = json.loads(clean[:end])
                        logger.warning(f"JSON 截断恢复成功（原始长度 {len(clean)}，截取 {end}）")
                        break
                    except json.JSONDecodeError:
                        continue
                else:
                    raise ValueError("无法恢复截断 JSON")
    except Exception as e:
        logger.warning(f"JSON 解析失败: {e}\n{result_text[:300]}")
        with db() as c:
            c.execute("UPDATE speakers SET last_analyzed_at=datetime('now'), pending_utterances=0 WHERE id=?",
                      (speaker_id,))
            c.commit()
        return

    with db() as c:
        # 更新意图分类
        for item in data.get("intents", []):
            c.execute("UPDATE transcripts SET directed_at_ai=? WHERE id=?",
                      (1 if item.get("directed_at_ai") else 0, item.get("id")))

        # 更新画像事实
        profiles = data.get("profiles", {})
        speaker_facts = profiles.get(speaker_id, {})
        for category, facts in speaker_facts.items():
            if not isinstance(facts, list): continue
            for fact in facts:
                if not fact or not isinstance(fact, str): continue
                fhash = hashlib.md5(f"{speaker_id}:{category}:{fact.lower()[:60]}".encode()).hexdigest()
                c.execute("""
                    INSERT INTO profile_facts(speaker_id,category,fact,fact_hash,updated_at)
                    VALUES(?,?,?,?,datetime('now'))
                    ON CONFLICT(fact_hash) DO UPDATE SET updated_at=datetime('now'), is_active=1
                """, (speaker_id, category, fact, fhash))
        c.commit()

    # 生成摘要（包含特征 + 发言内容）
    with db() as c:
        facts = c.execute("""
            SELECT category, fact FROM profile_facts
            WHERE speaker_id=? AND is_active=1
            ORDER BY category
        """, (speaker_id,)).fetchall()
        tx_rows = c.execute("""
            SELECT text FROM transcripts
            WHERE speaker_id=? AND is_filtered=0 AND (directed_at_ai=0 OR directed_at_ai=-1)
            ORDER BY ts DESC LIMIT 20
        """, (speaker_id,)).fetchall()

    facts_str = "\n".join([f"[{r['category']}] {r['fact']}" for r in facts]) if facts else "暂无提取特征"
    tx_str = "\n".join([f"- {r['text']}" for r in reversed(tx_rows)]) if tx_rows else "暂无发言记录"
    summary = await call_llm([{"role": "user", "content": SUMMARY_TMPL.format(
        speaker_id=speaker_id, facts=facts_str, transcripts=tx_str)}])

    with db() as c:
        if summary:
            c.execute("UPDATE speakers SET profile_summary=? WHERE id=?",
                      (summary.strip(), speaker_id))
        # 最后才更新 last_analyzed_at，作为分析完成的信号
        c.execute("UPDATE speakers SET last_analyzed_at=datetime('now'), pending_utterances=0 WHERE id=?",
                  (speaker_id,))
        c.commit()

    logger.info(f"分析完成: {speaker_id}，提取 {len(facts)} 条特征")

# ── 分析锁（防止同一说话人并发分析） ────────────────────────────────────────────
_analysis_locks: dict = {}

def _get_analysis_lock(speaker_id: str) -> asyncio.Lock:
    if speaker_id not in _analysis_locks:
        _analysis_locks[speaker_id] = asyncio.Lock()
    return _analysis_locks[speaker_id]

# ── 定时强制触发分析 ───────────────────────────────────────────────────────────
async def periodic_flush():
    """每 5 分钟强制分析有积累但未触发的说话人。"""
    while True:
        await asyncio.sleep(BATCH_MAX_WAIT_SEC)
        try:
            with db() as c:
                rows = c.execute("""
                    SELECT id FROM speakers
                    WHERE pending_utterances > 0
                    AND (last_analyzed_at IS NULL
                         OR (julianday('now') - julianday(last_analyzed_at)) * 86400 > ?)
                """, (BATCH_MAX_WAIT_SEC,)).fetchall()
            for r in rows:
                asyncio.create_task(run_batch_analysis(r['id']))
        except Exception as e:
            logger.warning(f"定时分析异常: {e}")

# ── API ───────────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    init_db()
    asyncio.create_task(periodic_flush())
    logger.info("Persona Agent 已启动")

@app.get("/api/health")
async def health():
    return {"status": "ok"}

@app.post("/api/segment")
async def receive_segment(seg: SegmentIn, bg: BackgroundTasks):
    """接收 VAD 转发的语音片段。"""
    text = (seg.text or "").strip()
    if not text:
        return {"status": "skip"}

    uid = seg.utterance_id or str(uuid.uuid4())
    speaker_id = seg.speaker_id

    # 未知说话人：尝试声纹注册
    if not speaker_id:
        if seg.audio_b64 and seg.duration_ms and seg.duration_ms >= UNKNOWN_MIN_DURATION:
            speaker_id = await enroll_unknown(seg.audio_b64)
        if not speaker_id:
            # 按 conv_id 暂存为 session 级未知人
            short = (seg.conv_id or "tmp")[:8]
            speaker_id = f"未知-{short}"

    ts = seg.ts or datetime.utcnow().isoformat()
    expires = (datetime.utcnow() + timedelta(minutes=STM_TTL_MINUTES)).isoformat()

    with db() as c:
        # 确保 speaker 存在
        c.execute("""INSERT OR IGNORE INTO speakers(id,display_name,is_registered,avatar_emoji)
                     VALUES(?,?,?,?)""",
                  (speaker_id, speaker_id,
                   1 if not speaker_id.startswith("未知-") else 0,
                   '👤' if not speaker_id.startswith("未知-") else '❓'))

        # 幂等插入对话记录
        try:
            c.execute("""INSERT INTO transcripts(utterance_id,speaker_id,text,ts,conv_id,duration_ms)
                         VALUES(?,?,?,?,?,?)""",
                      (uid, speaker_id, text, ts, seg.conv_id, seg.duration_ms))
        except sqlite3.IntegrityError:
            return {"status": "dup", "uid": uid}

        # 短期记忆
        c.execute("""INSERT INTO short_term_memory(speaker_id,conv_id,content,ts,expires_at)
                     VALUES(?,?,?,?,?)""",
                  (speaker_id, seg.conv_id, text, ts, expires))
        c.execute("DELETE FROM short_term_memory WHERE expires_at < datetime('now')")

        # 更新统计
        c.execute("""UPDATE speakers SET
                        last_seen=datetime('now'),
                        total_utterances=total_utterances+1,
                        pending_utterances=pending_utterances+1
                     WHERE id=?""", (speaker_id,))

        # 检查是否触发分析
        row = c.execute("""SELECT pending_utterances, last_analyzed_at FROM speakers WHERE id=?""",
                        (speaker_id,)).fetchone()
        trigger = False
        if row:
            pending = row['pending_utterances']
            last = row['last_analyzed_at']
            cooldown_ok = (not last or
                           (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds() > BATCH_DEBOUNCE_SEC)
            trigger = pending >= BATCH_MIN_UTTERANCES and cooldown_ok
        c.commit()

    if trigger:
        bg.add_task(run_batch_analysis, speaker_id)

    return {"status": "ok", "speaker_id": speaker_id, "uid": uid}

@app.get("/api/profiles")
async def list_profiles():
    with db() as c:
        rows = c.execute("""
            SELECT s.*,
                (SELECT COUNT(*) FROM profile_facts WHERE speaker_id=s.id AND is_active=1) AS fact_count,
                (SELECT COUNT(*) FROM short_term_memory WHERE speaker_id=s.id AND expires_at>datetime('now')) AS stm_count,
                (SELECT COUNT(*) FROM transcripts WHERE speaker_id=s.id AND directed_at_ai=0 AND is_filtered=0) AS human_count,
                (SELECT COUNT(*) FROM transcripts WHERE speaker_id=s.id AND directed_at_ai=1) AS ai_count
            FROM speakers s ORDER BY s.last_seen DESC
        """).fetchall()
    return [dict(r) for r in rows]

@app.get("/api/profile/{speaker_id:path}")
async def get_profile(speaker_id: str):
    with db() as c:
        sp = c.execute("SELECT * FROM speakers WHERE id=?", (speaker_id,)).fetchone()
        if not sp:
            raise HTTPException(404)
        facts = c.execute("""
            SELECT * FROM profile_facts WHERE speaker_id=? AND is_active=1
            ORDER BY category, confidence DESC
        """, (speaker_id,)).fetchall()
        transcripts = c.execute("""
            SELECT * FROM transcripts WHERE speaker_id=? ORDER BY ts DESC LIMIT 60
        """, (speaker_id,)).fetchall()
        stm = c.execute("""
            SELECT * FROM short_term_memory
            WHERE speaker_id=? AND expires_at>datetime('now')
            ORDER BY ts DESC LIMIT 20
        """, (speaker_id,)).fetchall()
    return {
        "speaker": dict(sp),
        "facts": [dict(f) for f in facts],
        "transcripts": [dict(t) for t in transcripts],
        "short_term_memory": [dict(s) for s in stm],
    }

@app.put("/api/transcript/{tid}/filter")
async def filter_transcript(tid: int, req: FilterReq):
    with db() as c:
        c.execute("UPDATE transcripts SET is_filtered=1, filter_reason=? WHERE id=?",
                  (req.reason, tid))
        c.commit()
    return {"status": "ok"}

@app.put("/api/transcript/{tid}/unfilter")
async def unfilter_transcript(tid: int):
    with db() as c:
        c.execute("UPDATE transcripts SET is_filtered=0, filter_reason=NULL WHERE id=?", (tid,))
        c.commit()
    return {"status": "ok"}

@app.post("/api/analyze/{speaker_id:path}")
async def trigger_analysis(speaker_id: str, bg: BackgroundTasks):
    with db() as c:
        if not c.execute("SELECT id FROM speakers WHERE id=?", (speaker_id,)).fetchone():
            raise HTTPException(404)
    bg.add_task(run_batch_analysis, speaker_id)
    return {"status": "queued"}

@app.post("/api/profile/{speaker_id:path}/merge/{target_id:path}")
async def merge_profiles(speaker_id: str, target_id: str):
    """将 speaker_id 合并到 target_id。"""
    with db() as c:
        for s in [speaker_id, target_id]:
            if not c.execute("SELECT id FROM speakers WHERE id=?", (s,)).fetchone():
                raise HTTPException(404, f"未找到: {s}")
        for tbl in ("transcripts", "profile_facts", "short_term_memory"):
            c.execute(f"UPDATE {tbl} SET speaker_id=? WHERE speaker_id=?", (target_id, speaker_id))
        c.execute("DELETE FROM speakers WHERE id=?", (speaker_id,))
        c.commit()
    return {"status": "merged", "surviving": target_id}

@app.delete("/api/profile/{speaker_id:path}")
async def delete_profile(speaker_id: str):
    with db() as c:
        for tbl in ("transcripts", "profile_facts", "short_term_memory"):
            c.execute(f"DELETE FROM {tbl} WHERE speaker_id=?", (speaker_id,))
        c.execute("DELETE FROM speakers WHERE id=?", (speaker_id,))
        c.commit()
    return {"status": "deleted"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8009)
