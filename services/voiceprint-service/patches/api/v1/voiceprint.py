from fastapi import APIRouter, File, UploadFile, Form, HTTPException, Depends
from fastapi.security import HTTPBearer
from typing import List
import time
from ...models.voiceprint import VoiceprintRegisterResponse, VoiceprintIdentifyResponse
from ...services.voiceprint_service import voiceprint_service
from ...database.voiceprint_db import voiceprint_db
from ...api.dependencies import AuthorizationToken
from ...core.logger import get_logger

security = HTTPBearer(description="接口令牌")
logger = get_logger(__name__)
router = APIRouter()


@router.get(
    "/speakers",
    summary="列出已注册说话人",
    description="返回所有已注册说话人的 ID、样本数及时间戳",
    dependencies=[Depends(security)],
)
async def list_speakers(token: AuthorizationToken):
    """列出所有已注册说话人"""
    speakers = voiceprint_db.list_speakers()
    return {"speakers": speakers, "total": len(speakers)}


@router.post(
    "/register",
    summary="声纹注册",
    response_model=VoiceprintRegisterResponse,
    description="注册或累积更新声纹特征。accumulate=true 时与已有嵌入加权平均，提高多环境准确率。",
    dependencies=[Depends(security)],
)
async def register_voiceprint(
    token: AuthorizationToken,
    speaker_id: str = Form(..., description="说话人ID"),
    file: UploadFile = File(..., description="WAV音频文件"),
    accumulate: bool = Form(False, description="True=累积平均（追加样本），False=覆盖重置"),
):
    try:
        if not file.filename.lower().endswith(".wav"):
            raise HTTPException(status_code=400, detail="只支持WAV格式音频文件")
        audio_bytes = await file.read()
        success = voiceprint_service.register_voiceprint(speaker_id, audio_bytes, accumulate=accumulate)
        if success:
            count = voiceprint_db._get_sample_count(speaker_id)
            mode = f"累积第{count}个样本" if accumulate else "覆盖"
            return VoiceprintRegisterResponse(success=True, msg=f"已登记({mode}): {speaker_id}")
        else:
            raise HTTPException(status_code=500, detail="声纹注册失败")
    except HTTPException:
        raise
    except Exception as e:
        logger.fail(f"声纹注册异常: {e}")
        raise HTTPException(status_code=500, detail=f"声纹注册失败: {str(e)}")


@router.post(
    "/identify",
    summary="声纹识别",
    response_model=VoiceprintIdentifyResponse,
    description="识别音频中的说话人",
    dependencies=[Depends(security)],
)
async def identify_voiceprint(
    token: AuthorizationToken,
    speaker_ids: str = Form("", description="候选说话人ID，逗号分隔（留空则搜索全部）"),
    file: UploadFile = File(..., description="WAV音频文件"),
):
    start_time = time.time()
    logger.info(f"开始声纹识别请求 - 候选说话人: {speaker_ids}, 文件: {file.filename}")
    try:
        if not file.filename.lower().endswith(".wav"):
            raise HTTPException(status_code=400, detail="只支持WAV格式音频文件")
        candidate_ids = [x.strip() for x in speaker_ids.split(",") if x.strip()] or None
        audio_bytes = await file.read()
        match_name, match_score = voiceprint_service.identify_voiceprint(candidate_ids, audio_bytes)
        total_time = time.time() - start_time
        logger.info(f"识别完成 {total_time:.3f}s → {match_name} ({match_score:.4f})")
        return VoiceprintIdentifyResponse(speaker_id=match_name, score=match_score)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"声纹识别异常: {e}")
        raise HTTPException(status_code=500, detail=f"声纹识别失败: {str(e)}")


@router.delete(
    "/{speaker_id}",
    summary="删除声纹",
    description="删除指定说话人的声纹特征",
    dependencies=[Depends(security)],
)
async def delete_voiceprint(
    token: AuthorizationToken,
    speaker_id: str,
):
    try:
        success = voiceprint_service.delete_voiceprint(speaker_id)
        if success:
            return {"success": True, "msg": f"已删除: {speaker_id}"}
        else:
            raise HTTPException(status_code=404, detail=f"未找到说话人: {speaker_id}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除声纹异常 {speaker_id}: {e}")
        raise HTTPException(status_code=500, detail=f"删除声纹失败: {str(e)}")
