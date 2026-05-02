#!/usr/bin/env bash
# 初始化脚本：创建 Docker 网络并按顺序启动所有服务

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info() { echo -e "${YELLOW}[INFO]${NC} $1"; }
ok()   { echo -e "${GREEN}[OK]${NC}   $1"; }

echo ""
echo "══════════════════════════════════════════════"
echo "  🚀  语音管道服务初始化"
echo "══════════════════════════════════════════════"
echo ""

# 1. 创建共享 Docker 网络
info "创建 Docker 网络 voice-pipeline..."
docker network create voice-pipeline 2>/dev/null && ok "网络 voice-pipeline 已创建" \
    || ok "网络 voice-pipeline 已存在，跳过"

echo ""

# 2. 启动 FunASR
info "启动 FunASR 服务（首次需要下载模型，约 2~5 分钟）..."
cd "$PROJECT_DIR/services/funasr-service"
docker compose up -d
ok "FunASR 容器已启动"

echo ""

# 3. 启动 voiceprint-api
info "启动 voiceprint-api 服务（含 MySQL）..."
cd "$PROJECT_DIR/services/voiceprint-service"
docker compose up -d
ok "voiceprint-api 容器已启动"

echo ""

# 4. 等待 FunASR 就绪（模型加载需要时间）
info "等待 FunASR 模型加载（最多 5 分钟）..."
echo "  可以用 'docker logs funasr -f' 查看进度"
TIMEOUT=300
ELAPSED=0
while ! (docker exec funasr ss -tlnp 2>/dev/null | grep -q 10095); do
    sleep 5
    ELAPSED=$((ELAPSED + 5))
    echo -ne "  等待中... ${ELAPSED}s / ${TIMEOUT}s\r"
    if [ $ELAPSED -ge $TIMEOUT ]; then
        echo ""
        echo -e "\033[1;33m[WARN]\033[0m FunASR 加载超时，请手动检查: docker logs funasr --tail 30"
        break
    fi
done
echo ""
ok "FunASR 已就绪（端口 10095 开放）"

echo ""

# 5. 等待 voiceprint-api 就绪
info "等待 voiceprint-api 就绪..."
ELAPSED=0
while ! curl -sf http://localhost:8005/health > /dev/null 2>&1 \
      && ! curl -sf http://localhost:8005/ > /dev/null 2>&1; do
    sleep 5
    ELAPSED=$((ELAPSED + 5))
    echo -ne "  等待中... ${ELAPSED}s\r"
    if [ $ELAPSED -ge 120 ]; then
        echo ""
        echo -e "\033[1;33m[WARN]\033[0m voiceprint-api 启动超时，请检查: docker logs voiceprint-api --tail 30"
        break
    fi
done
echo ""
ok "voiceprint-api 已就绪（端口 8005 开放）"

echo ""

# 6. 启动 VAD 服务
info "启动 VAD 服务..."
cd "$PROJECT_DIR/services/vad-service"
docker compose up -d --build
ok "VAD 服务容器已启动"

# 等待 VAD 就绪
ELAPSED=0
while ! (docker exec vad-service echo ok > /dev/null 2>&1); do
    sleep 2
    ELAPSED=$((ELAPSED + 2))
    if [ $ELAPSED -ge 60 ]; then
        break
    fi
done
sleep 5
ok "VAD 服务已启动（端口 8765 开放）"

echo ""
# 7. 启动 TTS 服务
info "启动 TTS 服务（MiniMax 后端）..."
cd "$PROJECT_DIR/services/tts-service"
if [ ! -f .env ]; then
    echo -e "\033[1;31m[ERROR]\033[0m tts-service/.env 不存在，请先复制并填写:"
    echo "  cp services/tts-service/.env.example services/tts-service/.env"
    echo "  然后填入 MINIMAX_API_KEY"
    exit 1
fi
docker compose up -d --build
ok "TTS 服务已启动（端口 8766）"

# 等待 TTS 就绪
ELAPSED=0
while ! curl -sf http://localhost:8766/health > /dev/null 2>&1; do
    sleep 3
    ELAPSED=$((ELAPSED + 3))
    if [ $ELAPSED -ge 60 ]; then
        echo -e "\033[1;33m[WARN]\033[0m TTS 服务启动超时，请检查: docker logs tts-service --tail 20"
        break
    fi
done
ok "TTS 服务已就绪（端口 8766 开放）"

echo ""
echo "══════════════════════════════════════════════"
echo ""
ok "所有服务已启动！运行验证测试："
echo ""
echo "  bash tests/run_all_tests.sh"
echo ""
echo "服务状态："
docker ps --format "  {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep -E "funasr|voiceprint|vad|tts" || true
echo ""
echo "══════════════════════════════════════════════"
