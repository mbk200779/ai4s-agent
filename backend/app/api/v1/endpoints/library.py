"""
论文库管理接口：看清单、删单篇、删多篇。

路由刻意放在 `/library` 而不是塞进 `/papers`：`/papers/{arxiv_id}/pdf` 那组
是按「论文标识」访问内容的运行时接口，而这里管的是「库里有哪些行」，
两者的失败模式不同（前者 404 是内容缺失，后者 409 是状态不允许），
混在一个前缀下会让客户端难以区分。
"""
from typing import List

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services import library_service

router = APIRouter(prefix="/library", tags=["论文库"])


class DeleteRequest(BaseModel):
    ids: List[str] = Field(default_factory=list, description="要删除的 paper_id 列表")


@router.get("/papers")
async def list_library_papers(
    project_id: str | None = None,
    q: str | None = Query(None, description="按标题 / arXiv ID / 作者模糊筛选"),
    limit: int = Query(500, ge=1, le=2000),
):
    """
    库内论文清单 + 汇总计数。

    `summary` 和 `items` 一起返回，是为了让前端的「论文数量」与「列表」
    永远来自同一时刻的快照——分成两个接口时，删除后两个请求如果间隔返回，
    会出现「数量已经变了、列表还没变」的错觉。
    """
    return library_service.list_papers(project_id=project_id, query=q, limit=limit)


@router.delete("/papers/{paper_id}")
async def delete_library_paper(paper_id: str):
    """删除单篇论文（含向量、元数据点、PDF 原件）。"""
    try:
        return library_service.delete_papers([paper_id])
    except library_service.PaperNotFound as exc:
        raise HTTPException(status_code=404, detail=f"论文不存在: {', '.join(exc.missing)}")
    except library_service.PapersBusy as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "有论文正在解析，暂不能删除",
                "blocked": exc.blocked,
            },
        )


@router.post("/papers/delete")
async def batch_delete_library_papers(req: DeleteRequest):
    """
    批量删除。

    用 POST 而不是带 body 的 DELETE：部分代理/网关会丢掉 DELETE 的请求体，
    表现为「点了删除没反应」——排查这类问题非常费时间，不如直接避开。
    """
    if not req.ids:
        raise HTTPException(status_code=400, detail="ids 不能为空")
    try:
        return library_service.delete_papers(req.ids)
    except library_service.PaperNotFound as exc:
        raise HTTPException(status_code=404, detail=f"论文不存在: {', '.join(exc.missing)}")
    except library_service.PapersBusy as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "有论文正在解析，暂不能删除",
                "blocked": exc.blocked,
            },
        )
