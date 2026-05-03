import numpy as np
import time
from typing import Dict, List, Optional
from .connection import db_connection
from ..core.logger import get_logger

logger = get_logger(__name__)


class VoiceprintDB:
    """声纹数据库操作类，支持多样本累积平均以提高识别精度"""

    def __init__(self):
        self._ensure_sample_count_column()

    def _ensure_sample_count_column(self):
        """启动时检查 sample_count 列是否存在，不存在则自动添加（幂等）"""
        try:
            with db_connection.get_cursor() as cursor:
                cursor.execute(
                    "ALTER TABLE voiceprints ADD COLUMN sample_count INT NOT NULL DEFAULT 1"
                )
                logger.info("已添加 sample_count 列到 voiceprints 表")
        except Exception:
            pass  # 列已存在，忽略

    # ──────────────────────────────────────────────────────────────────
    # 写操作
    # ──────────────────────────────────────────────────────────────────

    def save_voiceprint(
        self, speaker_id: str, emb: np.ndarray, accumulate: bool = False
    ) -> bool:
        """
        保存声纹特征。

        accumulate=False（默认）：直接覆盖，重置样本数为 1。
        accumulate=True         ：与已有嵌入做加权平均，样本数 +1，精度随样本数提升。
        """
        try:
            if accumulate:
                existing = self.get_voiceprints([speaker_id])
                if speaker_id in existing:
                    old_emb = existing[speaker_id]
                    old_count = self._get_sample_count(speaker_id)
                    # 加权平均：旧嵌入权重 = old_count，新样本权重 = 1
                    merged = (old_count * old_emb + emb) / (old_count + 1)
                    norm = np.linalg.norm(merged)
                    if norm > 0:
                        merged = merged / norm
                    new_count = old_count + 1
                    return self._update_voiceprint(speaker_id, merged, new_count)
                # 不存在则直接保存（与 accumulate=False 相同）

            with db_connection.get_cursor() as cursor:
                sql = """
                    INSERT INTO voiceprints (speaker_id, feature_vector, sample_count)
                    VALUES (%s, %s, 1)
                    ON DUPLICATE KEY UPDATE
                        feature_vector = VALUES(feature_vector),
                        sample_count   = 1
                """
                cursor.execute(sql, (speaker_id, emb.tobytes()))
                logger.success(f"声纹特征保存成功: {speaker_id}")
                return True
        except Exception as e:
            logger.fail(f"保存声纹特征失败 {speaker_id}: {e}")
            return False

    def _update_voiceprint(self, speaker_id: str, emb: np.ndarray, count: int) -> bool:
        try:
            with db_connection.get_cursor() as cursor:
                cursor.execute(
                    "UPDATE voiceprints SET feature_vector=%s, sample_count=%s WHERE speaker_id=%s",
                    (emb.tobytes(), count, speaker_id),
                )
                logger.success(f"声纹累积更新成功 (共{count}个样本): {speaker_id}")
                return True
        except Exception as e:
            logger.fail(f"更新声纹特征失败 {speaker_id}: {e}")
            return False

    # ──────────────────────────────────────────────────────────────────
    # 读操作
    # ──────────────────────────────────────────────────────────────────

    def _get_sample_count(self, speaker_id: str) -> int:
        try:
            with db_connection.get_cursor() as cursor:
                cursor.execute(
                    "SELECT sample_count FROM voiceprints WHERE speaker_id=%s",
                    (speaker_id,),
                )
                row = cursor.fetchone()
                return int(row[0]) if row else 1
        except Exception:
            return 1

    def get_voiceprints(
        self, speaker_ids: Optional[List[str]] = None
    ) -> Dict[str, np.ndarray]:
        start_time = time.time()
        query_type = (
            f"指定ID查询({len(speaker_ids)}个)" if speaker_ids else "全量查询"
        )
        logger.info(f"开始数据库查询: {query_type}")
        try:
            with db_connection.get_cursor() as cursor:
                if speaker_ids:
                    fmt = ",".join(["%s"] * len(speaker_ids))
                    cursor.execute(
                        f"SELECT speaker_id, feature_vector FROM voiceprints WHERE speaker_id IN ({fmt})",
                        tuple(speaker_ids),
                    )
                else:
                    cursor.execute("SELECT speaker_id, feature_vector FROM voiceprints")
                results = cursor.fetchall()
                voiceprints = {
                    row[0]: np.frombuffer(row[1], dtype=np.float32) for row in results
                }
                logger.info(
                    f"获取到 {len(voiceprints)} 个声纹特征，耗时: {time.time()-start_time:.3f}s"
                )
                return voiceprints
        except Exception as e:
            logger.error(f"获取声纹特征失败: {e}")
            return {}

    def list_speakers(self) -> List[dict]:
        """列出所有已注册说话人及其元数据（用于管理界面）"""
        try:
            with db_connection.get_cursor() as cursor:
                cursor.execute(
                    """SELECT speaker_id, sample_count, created_at, updated_at
                       FROM voiceprints ORDER BY updated_at DESC"""
                )
                rows = cursor.fetchall()
                return [
                    {
                        "speaker_id": r[0],
                        "sample_count": int(r[1]) if r[1] else 1,
                        "created_at": str(r[2]) if r[2] else None,
                        "updated_at": str(r[3]) if r[3] else None,
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.error(f"列出说话人失败: {e}")
            return []

    def delete_voiceprint(self, speaker_id: str) -> bool:
        try:
            with db_connection.get_cursor() as cursor:
                cursor.execute(
                    "DELETE FROM voiceprints WHERE speaker_id = %s", (speaker_id,)
                )
                if cursor.rowcount > 0:
                    logger.info(f"声纹特征删除成功: {speaker_id}")
                    return True
                else:
                    logger.warning(f"未找到要删除的声纹特征: {speaker_id}")
                    return False
        except Exception as e:
            logger.error(f"删除声纹特征失败 {speaker_id}: {e}")
            return False

    def count_voiceprints(self) -> int:
        try:
            with db_connection.get_cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM voiceprints")
                result = cursor.fetchone()
                return result[0] if result else 0
        except Exception as e:
            logger.error(f"获取声纹特征总数失败: {e}")
            return 0


# 全局声纹数据库操作实例
voiceprint_db = VoiceprintDB()
