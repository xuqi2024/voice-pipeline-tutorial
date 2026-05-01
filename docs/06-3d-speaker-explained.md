# 3D-Speaker 与 voiceprint-api 的关系及实现原理

## 概述

```
modelscope/3D-Speaker
        │
        │ 提供预训练模型 (CAM++, ERes2Net 等)
        │ 提供声纹提取算法和推理代码
        ▼
xinnan-tech/voiceprint-api
        │
        │ 封装为 REST API 服务
        │ 添加数据库存储、注册/识别接口
        ▼
你的应用
```

**关系**: voiceprint-api 是 3D-Speaker 的**工程化封装**。3D-Speaker 提供学术级的模型和算法，voiceprint-api 将其包装成可直接调用的微服务。

---

## 3D-Speaker 是什么

3D-Speaker 是阿里达摩院开源的**说话人验证和识别**工具包：

- **说话人验证** (Speaker Verification): 判断两段音频是否来自同一人（1:1 比对）
- **说话人识别** (Speaker Recognition): 从已知人员库中找到最匹配的人（1:N 比对）
- **说话人分离** (Speaker Diarization): 在多人对话中区分不同说话人（多说话人）

我们的场景是**说话人识别 (1:N)**。

---

## 核心算法：声纹嵌入（Speaker Embedding）

### 基本思路

1. 将音频转换为**声学特征**（Mel 频谱图）
2. 用神经网络提取**声纹嵌入向量**（d-vector，通常 192 或 256 维）
3. 相同说话人的嵌入向量**余弦相似度高**，不同说话人**余弦相似度低**

```
音频 (WAV)
    │
    ▼
MFCC / Mel 频谱特征提取
    │
    ▼
神经网络编码器 (CAM++ / ERes2Net)
    │
    ▼
声纹嵌入向量 (192维 float32)
例: [0.23, -0.11, 0.87, ..., 0.04]
```

### CAM++ 模型结构

voiceprint-api 默认使用 CAM++ 模型（`iic/speech_campplus_sv_zh-cn_16k-common`）：

```
输入: Mel 频谱 (80维, T帧)
  │
  ▼
TDNN (Time Delay Neural Network) 层
  │ 提取局部时序特征
  ▼
CAM (Context-Aware Masking) 模块
  │ 自适应上下文聚合，区分有效帧和静音帧
  ▼
统计池化 (Attentive Statistics Pooling)
  │ 将变长序列压缩为固定长度向量 (均值+标准差)
  ▼
全连接层 + 归一化
  │
  ▼
声纹嵌入向量 (192维)
```

**CAM 的创新点**: 传统模型平等对待所有帧，CAM 通过注意力机制让模型专注于信息量丰富的帧（有实际语音的帧），忽略静音和噪声帧，从而在嘈杂环境下更鲁棒。

---

## 相似度计算

### 余弦相似度

```python
import numpy as np

def cosine_similarity(vec1, vec2):
    """计算两个声纹向量的相似度"""
    vec1 = vec1 / np.linalg.norm(vec1)  # L2 归一化
    vec2 = vec2 / np.linalg.norm(vec2)
    return np.dot(vec1, vec2)  # 范围: -1 ~ 1，越高越相似

# 示例
score = cosine_similarity(query_embedding, registered_embedding)
# score > 0.7 → 同一人
```

### 1:N 识别流程

```python
# voiceprint-api 内部逻辑（简化版）
def identify_speaker(audio_wav):
    # 1. 提取未知音频的声纹向量
    query_emb = model.extract_embedding(audio_wav)
    
    # 2. 与数据库中所有注册声纹比对
    best_score = -1
    best_speaker = None
    for speaker_id, stored_emb in database.items():
        score = cosine_similarity(query_emb, stored_emb)
        if score > best_score:
            best_score = score
            best_speaker = speaker_id
    
    # 3. 阈值判断
    if best_score >= threshold:  # 默认 0.7
        return {"speaker_id": best_speaker, "score": best_score}
    else:
        return {"speaker_id": None, "score": best_score}
```

---

## 3D-Speaker 数据集

3D-Speaker 不仅是工具包，还发布了同名数据集：

- **规模**: 10,000+ 说话人，250,000+ 句子
- **特色**: 同一说话人在**不同场景**（安静室内、嘈杂室内、走廊、室外）录制
- **目的**: 训练出对环境鲁棒的声纹模型

这就是名字中 "3D" 的含义：**跨设备、跨场景、跨距离**（cross-device, cross-scene, cross-distance）。

---

## 模型性能对比

| 模型 | 参数量 | 等错率 (EER) | 特点 |
|------|--------|------------|------|
| CAM++ | 7.2M | 0.65% | 轻量，速度快，**voiceprint-api 默认** |
| ERes2Net-base | 6.6M | 0.84% | 均衡 |
| ERes2NetV2 | 17.8M | 0.61% | 更准确，更慢 |
| ECAPA-TDNN | 20.8M | 0.86% | 经典模型 |

EER（等错误率）越低越好：当误接受率=误拒绝率时的错误率。

---

## 在 voiceprint-api 中切换模型

修改 `data/.voiceprint.yaml`：

```yaml
model:
  # CAM++ (默认，推荐)
  model_id: "iic/speech_campplus_sv_zh-cn_16k-common"
  
  # ERes2NetV2 (更高精度，需要更多 RAM)
  # model_id: "iic/speech_eres2netv2_sv_zh-cn_16k-common"
  
  threshold: 0.7  # 识别阈值，越高越严格
```
