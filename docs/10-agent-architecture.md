# 智能体架构设计

> 从当前 `llm-service` 出发，设计支持 Tools / Skills / Multi-Agent / MCP / A2A 的下一代智能体，并适配 ESP32 端侧运行。

---

## 一、当前 llm-service 剖析

### 组件地图

```
llm-service/app.py
├── 配置层
│   ├── MINIMAX_API_KEY / MINIMAX_MODEL        # LLM 提供商
│   ├── TTS_SERVICE_URL / TTS_VOICE_ID         # TTS 调用
│   ├── VAD_SERVICE_URL                         # 回传设备
│   └── HOST_IP                                 # TTS 音频下载地址
│
├── 状态层
│   ├── histories: dict[speaker → deque(20)]   # 每人独立对话历史（固定窗口）
│   └── _audio_cache: dict[id → bytes]         # TTS WAV 缓存（最多 20 条）
│
├── API 层（FastAPI）
│   ├── POST /api/chat                          # 主入口：接收文本 → LLM → TTS
│   ├── GET  /api/audio/{id}                    # 提供 TTS WAV 下载
│   ├── DELETE /api/history/{speaker}           # 清除对话历史
│   └── GET  /api/health                        # 健康检查
│
├── LLM 调用
│   ├── Anthropic 兼容格式（MiniMax M2.7）
│   ├── system prompt 注入说话人姓名
│   └── messages 包含历史对话（最近 10 轮）
│
├── TTS 调用（call_tts）
│   ├── POST /tts → tts-service
│   ├── 限流重试（3次，间隔 3s）
│   └── WAV 缓存 + 计算时长（供回声检测）
│
└── 事件广播
    ├── push_dashboard()   → Dashboard SSE 展示
    └── forward_to_device("*", tts_url)  → VAD → 所有音频设备播放
```

### 当前局限

| 问题 | 说明 |
|------|------|
| **无 Tool 调用** | LLM 只能纯文本对话，无法查天气、搜索、控制设备 |
| **无规划能力** | 单步 LLM 调用，不能拆解复杂任务 |
| **无长期记忆** | 重启后对话历史全丢，无持久化 |
| **无多 Agent 协作** | 单一 LLM，无法分派子任务给专业 Agent |
| **紧耦合** | LLM / TTS / 设备广播全在一个文件 |
| **无协议标准** | 设备通信是自定义 JSON，无 MCP/A2A 支持 |

---

## 二、现代智能体协议栈（2025）

```
┌─────────────────────────────────────────────────────────────┐
│                    协议层 Protocol Stack                      │
├──────────────┬──────────────────────────┬───────────────────┤
│ MCP          │ A2A                       │ ACP               │
│ (Anthropic)  │ (Google, 2025)            │ (IBM/BeeAI)       │
│              │                           │                   │
│ Agent ↔ Tool │ Agent ↔ Agent             │ Agent ↔ 开发工具   │
│ 函数调用标准  │ 跨 Agent 任务委托          │ IDE/编排系统集成   │
│ JSON-RPC     │ HTTP/SSE + Agent Cards    │ 类似 LSP          │
└──────────────┴──────────────────────────┴───────────────────┘

MCP = 连接工具（USB-C 比喻）
A2A = 连接 Agent（HTTP 比喻）
ACP = 连接编排系统

三者互补，不是竞争关系：
  └→ Agent 内部用 MCP 调用 Tool
  └→ Agent 之间用 A2A 委托任务
  └→ 平台层用 ACP 管理 Agent 生命周期
```

---

## 三、目标架构：ESP32 端云协同智能体

```
                        ┌─────────────────────────────────────┐
                        │          云端 Agent Brain            │
  ┌──────────┐          │                                       │
  │ ESP32-S3 │ ─WS──→  │  ┌──────────────────────────────┐   │
  │ 麦克风   │          │  │    Orchestrator Agent         │   │
  │ 喇叭     │          │  │  (ReAct / CoT 规划引擎)        │   │
  │ OLED     │          │  └──────┬────────────────────────┘   │
  │ 传感器   │ ←──WS─   │         │ A2A 任务委派                │
  └──────────┘          │  ┌──────┴────────────────────────┐   │
                        │  │         Sub-Agents              │   │
                        │  │  ┌──────────┐ ┌────────────┐  │   │
                        │  │  │ 对话 Agent│ │ 工具 Agent  │  │   │
                        │  │  │ (闲聊/问答)│ │ (天气/搜索)│  │   │
                        │  │  └──────────┘ └─────┬──────┘  │   │
                        │  └──────────────────────┼─────────┘   │
                        │                          │ MCP         │
                        │  ┌───────────────────────▼──────────┐  │
                        │  │          Tool Registry            │  │
                        │  │  weather │ search │ home-control  │  │
                        │  │  calendar│ timer  │ device-ctrl   │  │
                        │  └──────────────────────────────────┘  │
                        │                                         │
                        │  ┌──────────────────────────────────┐  │
                        │  │          Memory Layer             │  │
                        │  │  短期: 对话上下文（窗口）           │  │
                        │  │  中期: 本次会话摘要                 │  │
                        │  │  长期: 向量数据库（用户偏好/知识）   │  │
                        │  └──────────────────────────────────┘  │
                        └─────────────────────────────────────────┘
```

---

## 四、ESP32 端：瘦客户端模型

ESP32 **不运行 LLM**，只负责：

```c
// ESP32 职责划分
typedef struct {
    // 感知层
    i2s_channel_handle_t  mic;         // INMP441 音频采集
    // 通信层
    esp_websocket_client  ws;          // 与云端 VAD/Agent 通信
    // 输出层
    i2s_channel_handle_t  speaker;     // MAX98357A 播放 TTS
    ssd1306_t             oled;        // 显示识别文字 / 状态
    // 动作层（ESP-CLAW 扩展）
    gpio_num_t            gpio_out[];  // 控制继电器/LED
} esp32_agent_client_t;
```

### ESP-CLAW 对应关系

| ESP-CLAW 概念 | 在本项目中的实现 |
|---------------|-----------------|
| **Conversation** | WebSocket → VAD → ASR → LLM 对话流 |
| **Logic** | 云端 Orchestrator Agent（ReAct 规划） |
| **Action** | ESP32 GPIO / 家电控制 Tool（MCP） |
| **World** | 传感器读数 / 状态上报到 Agent Memory |

---

## 五、下一代 Agent 代码架构（Python）

```
agent-service/
├── app.py                    # FastAPI 入口 + 路由
├── core/
│   ├── orchestrator.py       # 主 Agent：ReAct 规划循环
│   ├── memory.py             # 三层记忆（短/中/长期）
│   └── context.py            # 会话上下文管理
│
├── agents/                   # Sub-Agents（每个独立 LLM 实例）
│   ├── base.py               # BaseAgent 抽象类
│   ├── chat_agent.py         # 闲聊/问答 Agent
│   ├── tool_agent.py         # Tool 调用 Agent（MCP Client）
│   └── persona_agent.py      # 个性化/声纹绑定 Agent
│
├── tools/                    # MCP Tools（标准化接口）
│   ├── registry.py           # Tool 注册表
│   ├── weather.py            # 天气查询
│   ├── search.py             # 联网搜索
│   ├── timer.py              # 计时/提醒
│   ├── home_control.py       # ESP32 GPIO 控制（A2A → ESP32）
│   └── calendar.py           # 日程管理
│
├── protocols/
│   ├── mcp_server.py         # 暴露 MCP 服务端（供其他 Agent 调用本 Agent 的 Tool）
│   ├── a2a_client.py         # A2A 客户端（委托任务给其他 Agent）
│   └── a2a_server.py         # A2A 服务端（接受其他 Agent 委托）
│
├── skills/                   # 高阶技能（多步 Tool 组合）
│   ├── base.py
│   ├── weather_report.py     # 天气播报技能（查询→TTS→播放）
│   └── morning_brief.py      # 早报技能（天气+日历+新闻）
│
└── adapters/                 # 设备适配器
    ├── esp32_adapter.py      # ESP32 WebSocket 适配
    ├── tts_adapter.py        # TTS 服务适配
    └── vad_adapter.py        # VAD 事件适配
```

---

## 六、Orchestrator ReAct 规划循环

```
用户输入: "明天天气怎么样，如果下雨提醒我带伞"
            │
            ▼
┌─────────────────────────────────────┐
│         Orchestrator（ReAct）        │
│                                      │
│  思考 (Thought):                     │
│  "需要查天气，再设定提醒"             │
│                                      │
│  行动 (Action): call_tool            │
│    → weather(location="当前位置")    │
│                                      │
│  观察 (Observation):                 │
│    → {"tomorrow": "rain", "prob": 0.85} │
│                                      │
│  思考: "有雨，设置提醒"              │
│  行动: call_tool                     │
│    → timer(time="07:30", msg="带伞") │
│                                      │
│  观察: {"ok": true}                  │
│                                      │
│  思考: "任务完成，回复用户"           │
│  最终回答: "明天有雨概率85%..."      │
└─────────────────────────────────────┘
            │
            ▼
     TTS + ESP32 播放
```

---

## 七、三层记忆设计

```python
class AgentMemory:
    # 短期记忆：当前对话上下文（Token 窗口）
    working_memory: deque[Message]    # 最近 20 条，直接放进 prompt

    # 中期记忆：会话摘要（超出窗口后压缩）
    session_summary: str              # "用户叫徐琪，问过天气，设过提醒..."

    # 长期记忆：向量数据库（跨会话持久化）
    vector_store: ChromaDB / Milvus   # 存储用户偏好、重要事件、知识
    # 检索：相似度搜索，每次对话前 top-3 注入 prompt
```

---

## 八、MCP Tool 标准化接口

```python
# 每个 Tool 遵循 MCP 规范，用 JSON Schema 描述
class WeatherTool(MCPTool):
    name = "get_weather"
    description = "查询指定城市的天气预报"
    input_schema = {
        "type": "object",
        "properties": {
            "location": {"type": "string", "description": "城市名"},
            "days":     {"type": "integer", "description": "预报天数 1-7", "default": 1}
        },
        "required": ["location"]
    }

    async def execute(self, location: str, days: int = 1) -> dict:
        # 调用天气 API
        return {"weather": "rain", "temp": 18, "prob": 0.85}
```

---

## 九、Multi-Agent A2A 委托

```python
# Orchestrator 遇到需要专业能力时，A2A 委托给专门 Agent
class OrchestratorAgent:
    async def handle(self, text: str, speaker: str):
        intent = await self.classify_intent(text)

        if intent == "home_control":
            # A2A 委托给家居控制 Agent
            result = await self.a2a_client.delegate(
                target_agent="home-agent",
                task={"action": "turn_on", "device": "客厅灯"},
                callback_url="http://agent-service:8006/api/a2a/callback"
            )
        elif intent == "search":
            # A2A 委托给搜索 Agent
            result = await self.a2a_client.delegate(
                target_agent="search-agent",
                task={"query": text}
            )
        else:
            # 本地对话 Agent 处理
            result = await self.chat_agent.handle(text, speaker)
```

---

## 十、ESP32 侧 Agent 扩展

当你希望 ESP32 本地有一定"智能"时，可以在 ESP32 上运行**极简决策引擎**：

```c
// ESP32 本地 Agent（不含 LLM，基于规则 + 状态机）
typedef enum {
    AGENT_STATE_IDLE,       // 待机
    AGENT_STATE_LISTENING,  // 录音中
    AGENT_STATE_THINKING,   // 等待云端响应
    AGENT_STATE_SPEAKING,   // TTS 播放中
    AGENT_STATE_ACTING,     // 执行本地 Action（开灯等）
} agent_state_t;

// 本地可执行的 Actions（不需要云端）
typedef struct {
    const char* trigger;         // 关键词触发
    void (*action)(void);        // 本地执行函数
} local_action_t;

static local_action_t local_actions[] = {
    {"开灯",   gpio_turn_on_light},
    {"关灯",   gpio_turn_off_light},
    {"音量加", i2s_volume_up},
    {"音量减", i2s_volume_down},
    {NULL, NULL}
};

// 状态机：本地 Action 不需要走云端
void agent_process_asr(const char* text) {
    for (local_action_t* a = local_actions; a->trigger; a++) {
        if (strstr(text, a->trigger)) {
            a->action();    // 立即本地执行
            return;
        }
    }
    // 其余统一发往云端 Agent
    ws_send_to_cloud(text);
}
```

---

## 十一、演进路线

```
现在          v2.0             v3.0              v4.0
  │             │                │                 │
  ▼             ▼                ▼                 ▼
当前          Tool 支持        Multi-Agent       端侧 Agent
llm-service  (MCP 工具调用)   (A2A 协作)        (ESP32 本地决策)
  │             │                │                 │
单 LLM       ReAct 规划       专业子 Agent       轻量状态机
无 Tool      天气/搜索/定时   家居/日历/搜索     本地关键词响应
固定 Prompt  三层记忆         向量知识库         边缘推理（ESP-CLAW）
```

### v2.0 实现要点（最近可做）

1. **添加 Tool Registry** — 注册 2-3 个工具（天气、搜索）
2. **LLM Function Calling** — MiniMax 支持 tool_calls，改写 `/api/chat` 加入工具调用循环
3. **对话历史持久化** — 改写 `histories` 用 SQLite/Redis 存储
4. **Skill 封装** — 把"天气播报"封装成多步技能
5. **Dashboard 展示 Tool 调用** — 在事件流中展示 Agent 思考过程

---

## 参考项目

| 项目 | 说明 | 链接 |
|------|------|------|
| xiaozhi-esp32-server | 最接近本项目的参考实现 | [GitHub](https://github.com/xinnan-tech/xiaozhi-esp32-server) |
| ESP-CLAW | Espressif 官方 ESP32 Agent 框架 | CLAW = Conversation/Logic/Action/World |
| MCP (Anthropic) | Agent-to-Tool 标准协议 | [spec.modelcontextprotocol.io](https://spec.modelcontextprotocol.io) |
| A2A (Google) | Agent-to-Agent 标准协议 | [google.github.io/A2A](https://google.github.io/A2A) |
| LangChain / LangGraph | Tool 调用 + 多 Agent 编排 Python 框架 | [langchain.com](https://langchain.com) |
| AutoGen (Microsoft) | Multi-Agent 对话框架 | [github.com/microsoft/autogen](https://github.com/microsoft/autogen) |
