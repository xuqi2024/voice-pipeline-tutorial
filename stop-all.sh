#!/usr/bin/env bash
# 停止所有服务

cd "$(dirname "${BASH_SOURCE[0]}")"

echo "停止所有语音管道服务..."

docker compose -f services/tts-service/docker-compose.yml down 2>/dev/null || true
docker compose -f services/vad-service/docker-compose.yml down 2>/dev/null || true
docker compose -f services/voiceprint-service/docker-compose.yml down 2>/dev/null || true
docker compose -f services/funasr-service/docker-compose.yml down 2>/dev/null || true

echo "所有服务已停止"
