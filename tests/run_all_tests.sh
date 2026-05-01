#!/usr/bin/env bash
# 全链路测试脚本
# 按顺序验证所有服务，全部通过才算就绪

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# 颜色
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass() { echo -e "${GREEN}[PASS]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; }
info() { echo -e "${YELLOW}[INFO]${NC} $1"; }

echo ""
echo "════════════════════════════════════════════"
echo "  🧪  语音管道全链路测试"
echo "════════════════════════════════════════════"
echo ""

# 检查 Python 依赖
info "检查测试依赖..."
python3 -c "import websockets, httpx" 2>/dev/null || {
    info "安装测试依赖..."
    pip install websockets httpx -q
}

PASSED=0
FAILED=0

# ── 测试 1: FunASR ──────────────────────────────
echo "─────────────────────────────────────────────"
echo "[1/3] 测试 FunASR (ws://localhost:10095)"
echo "─────────────────────────────────────────────"
if python3 "$SCRIPT_DIR/test_funasr.py" --host localhost --port 10095; then
    pass "FunASR 验证通过"
    PASSED=$((PASSED + 1))
else
    fail "FunASR 验证失败"
    FAILED=$((FAILED + 1))
    echo ""
    info "排查建议:"
    echo "  cd $PROJECT_DIR/services/funasr-service"
    echo "  docker compose up -d"
    echo "  docker compose logs -f  # 等待模型加载完成"
fi

echo ""

# ── 测试 2: voiceprint-api ───────────────────────
echo "─────────────────────────────────────────────"
echo "[2/3] 测试 voiceprint-api (http://localhost:8005)"
echo "─────────────────────────────────────────────"
if python3 "$SCRIPT_DIR/test_voiceprint.py" --url http://localhost:8005; then
    pass "voiceprint-api 验证通过"
    PASSED=$((PASSED + 1))
else
    fail "voiceprint-api 验证失败"
    FAILED=$((FAILED + 1))
    echo ""
    info "排查建议:"
    echo "  cd $PROJECT_DIR/services/voiceprint-service"
    echo "  docker compose up -d"
    echo "  docker compose logs -f voiceprint-api"
fi

echo ""

# ── 测试 3: VAD Service ─────────────────────────
echo "─────────────────────────────────────────────"
echo "[3/3] 测试 VAD Service (ws://localhost:8765)"
echo "─────────────────────────────────────────────"
if python3 "$SCRIPT_DIR/test_vad.py" --url ws://localhost:8765; then
    pass "VAD 服务验证通过"
    PASSED=$((PASSED + 1))
else
    fail "VAD 服务验证失败"
    FAILED=$((FAILED + 1))
    echo ""
    info "排查建议:"
    echo "  cd $PROJECT_DIR/services/vad-service"
    echo "  docker compose up -d --build"
    echo "  docker compose logs -f"
fi

echo ""
echo "════════════════════════════════════════════"
echo ""

if [ "$FAILED" -eq 0 ]; then
    echo -e "${GREEN}✅ 所有服务验证通过！ ($PASSED/3)${NC}"
    echo ""
    echo "管道已就绪，可以启动麦克风客户端："
    echo ""
    echo "  cd $PROJECT_DIR/client"
    echo "  pip install -r requirements.txt"
    echo "  python mic_capture.py"
    echo ""
else
    echo -e "${RED}❌ 有 $FAILED 个服务验证失败 (通过: $PASSED/3)${NC}"
    echo ""
    echo "请先修复失败的服务，再运行麦克风客户端。"
    echo ""
    exit 1
fi

echo "════════════════════════════════════════════"
echo ""
