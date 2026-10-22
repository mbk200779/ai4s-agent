"""
评测报告接口：运行检查、列出历史报告、查看单条报告。
"""
from fastapi import APIRouter, HTTPException
from app.schemas.eval import EvalReport, EvalListResponse
from app.services.eval_service import EvalService

router = APIRouter(prefix="/eval", tags=["评测"])


@router.post("/run", response_model=EvalReport)
async def run_eval():
    """
    验收标准：
    1. 运行 API/Qdrant/MinIO 健康检查；
    2. 验证检索可复现性（同一查询两次 RRF 结果一致）；
    3. 把报告写入 MinIO provenance bucket 并返回 key。
    """
    return EvalService().run()


@router.get("/reports", response_model=EvalListResponse)
async def list_reports(limit: int = 50):
    """列出历史评测报告。"""
    return EvalListResponse(reports=EvalService().list_reports(limit=limit))


@router.get("/reports/{key:path}")
async def get_report(key: str):
    """根据 key 查看单条评测报告。"""
    try:
        return EvalService().get_report(key)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"报告不存在或读取失败：{e}")
