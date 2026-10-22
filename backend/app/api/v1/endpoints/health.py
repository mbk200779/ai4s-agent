"""
健康检查接口：确认后端及依赖可用性。
"""
from fastapi import APIRouter

router = APIRouter(prefix="/health", tags=["健康"])


@router.get("/")
async def health():
    """验收标准：返回 {status: ok, version: v1.0.0}。"""
    return {"status": "ok", "version": "v1.0.0"}
