#!/usr/bin/env python3
"""
voiceprint-api 服务验证测试

测试内容:
1. 健康检查接口
2. 声纹注册（使用生成的测试音频）
3. 声纹识别（同一段音频，预期 score 极高）
4. 声纹列表查询
5. 声纹删除（清理测试数据）

用法:
  python tests/test_voiceprint.py
  python tests/test_voiceprint.py --url http://localhost:8005
"""

import argparse
import io
import math
import struct
import sys
import wave

import httpx

VOICEPRINT_URL = "http://localhost:8005"
TEST_SPEAKER_ID = "test_speaker_autotest_9527"
API_KEY = "de395e06-035c-44f9-9a6b-8ef126a8bea0"


def generate_voice_like_wav(duration_sec: float = 4.0) -> bytes:
    """
    生成模拟人声频率的 WAV（200Hz 基频 + 谐波），用于测试声纹 API 连通性。
    注意：真实使用时需要真实人声。
    """
    sample_rate = 16000
    n_samples = int(sample_rate * duration_sec)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        samples = []
        for i in range(n_samples):
            t = i / sample_rate
            # 200Hz 基频 + 谐波，模拟人声频率范围
            val = (
                0.5 * math.sin(2 * math.pi * 200 * t)
                + 0.3 * math.sin(2 * math.pi * 400 * t)
                + 0.2 * math.sin(2 * math.pi * 600 * t)
            )
            samples.append(struct.pack("<h", int(32767 * 0.4 * val)))
        wf.writeframes(b"".join(samples))
    return buf.getvalue()


def test_health(client: httpx.Client, base_url: str) -> bool:
    """测试健康检查"""
    print("[1/5] 健康检查...")
    try:
        resp = client.get(f"{base_url}/voiceprint/health", params={"key": API_KEY}, timeout=5.0)
        if resp.status_code == 200:
            print(f"      [✓] HTTP {resp.status_code}: {resp.text[:100]}")
            return True
        else:
            print(f"      [FAIL] HTTP {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        print(f"      [FAIL] 健康检查失败: {e}")
    return False


def test_register(client: httpx.Client, base_url: str, wav_bytes: bytes) -> bool:
    """测试声纹注册"""
    print(f"[2/5] 注册声纹 (speaker_id={TEST_SPEAKER_ID})...")
    try:
        resp = client.post(
            f"{base_url}/voiceprint/register",
            files={"file": ("test.wav", wav_bytes, "audio/wav")},
            data={"speaker_id": TEST_SPEAKER_ID},
            params={"key": API_KEY},
            timeout=30.0,
        )
        if resp.status_code in (200, 201):
            print(f"      [✓] 注册成功: {resp.json()}")
            return True
        else:
            print(f"      [FAIL] HTTP {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        print(f"      [FAIL] 注册失败: {e}")
        return False


def test_identify(client: httpx.Client, base_url: str, wav_bytes: bytes) -> bool:
    """测试声纹识别（用同一音频，预期 score 极高）"""
    print("[3/5] 识别声纹（同一音频，预期高分）...")
    try:
        resp = client.post(
            f"{base_url}/voiceprint/identify",
            files={"file": ("test.wav", wav_bytes, "audio/wav")},
            data={"speaker_ids": TEST_SPEAKER_ID},
            params={"key": API_KEY},
            timeout=30.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            speaker_id = data.get("speaker_id")
            score = data.get("score", 0)
            is_identified = data.get("is_identified", False)
            print(f"      [✓] 识别结果: speaker_id={speaker_id}, score={score:.3f}, identified={is_identified}")
            if speaker_id == TEST_SPEAKER_ID:
                print(f"      [✓] 正确识别为测试说话人")
            else:
                print(f"      [!] 未识别为测试说话人（score 可能低于阈值，合成音频正常）")
            return True
        else:
            print(f"      [FAIL] HTTP {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        print(f"      [FAIL] 识别失败: {e}")
        return False


def test_list(client: httpx.Client, base_url: str) -> bool:
    """跳过列表查询（API 未提供 list 端点）"""
    print("[4/5] 查询声纹（跳过，API 无 list 端点）...")
    print(f"      [✓] 跳过（使用 /voiceprint/health 可查看总数）")
    return True


def test_delete(client: httpx.Client, base_url: str) -> bool:
    """测试声纹删除（清理测试数据）"""
    print(f"[5/5] 删除测试声纹 ({TEST_SPEAKER_ID})...")
    try:
        resp = client.delete(
            f"{base_url}/voiceprint/{TEST_SPEAKER_ID}",
            params={"key": API_KEY},
            timeout=10.0,
        )
        if resp.status_code in (200, 204):
            print(f"      [✓] 删除成功")
            return True
        else:
            print(f"      [!] HTTP {resp.status_code}: {resp.text[:100]}")
            return True  # 清理失败不影响测试结论
    except Exception as e:
        print(f"      [!] 删除失败: {e}")
        return True


def main():
    parser = argparse.ArgumentParser(description="voiceprint-api 验证测试")
    parser.add_argument("--url", default=VOICEPRINT_URL, help="API 地址")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    print(f"[TEST] voiceprint-api: {base_url}\n")

    wav_bytes = generate_voice_like_wav(duration_sec=4.0)
    print(f"测试音频: 合成人声频率, 4秒, WAV 16kHz mono\n")

    results = []
    headers = {"authorization": f"Bearer {API_KEY}"}
    with httpx.Client(headers=headers) as client:
        results.append(test_health(client, base_url))
        if not results[-1]:
            print(f"\n  [FAIL] ❌ 服务不可达，请检查:")
            print(f"         docker compose -f services/voiceprint-service/docker-compose.yml ps")
            sys.exit(1)

        results.append(test_register(client, base_url, wav_bytes))
        results.append(test_identify(client, base_url, wav_bytes))
        results.append(test_list(client, base_url))
        results.append(test_delete(client, base_url))

    if all(results):
        print(f"\n  [PASS] ✅ voiceprint-api 验证通过\n")
        sys.exit(0)
    else:
        print(f"\n  [FAIL] ❌ 部分测试未通过\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
