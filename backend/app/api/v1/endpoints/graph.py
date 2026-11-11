"""
引用图谱接口。

两套能力并存，各自答不同的问题：
- `/graph/build`、`/graph/{arxiv_id}/high-impact`：以一篇论文为中心，实时问 OpenAlex
  「外面谁引了它」；
- `/graph/library`：只读库内数据，回答「我收的这批论文之间是什么关系」，
  带 PageRank/betweenness 与一句式解读。
"""
from fastapi import APIRouter, HTTPException, Query

from app.schemas.graph import GraphEdge, GraphNode, GraphRequest, GraphResponse
from app.services.graph import get_service as get_library_service
from app.services.graph.backfill import rebuild_references
from app.services.graph_service import GraphService

router = APIRouter(prefix="/graph", tags=["图谱"])


# ---------------------------------------------------------------- 库内图谱


@router.get("/library")
async def library_graph(
    project_id: str | None = None,
    include_gaps: bool = True,
    max_gaps: int = Query(8, ge=0, le=30),
    min_gap_citers: int = Query(2, ge=1, le=10),
):
    """
    库内引用图谱快照。只读：不改库、不写匹配结果。

    `include_gaps` 控制是否把「库内多篇引用、但库里没有」的文献画成缺口节点——
    这是小库唯一能把图连起来的来源，默认开。
    """
    from app.repositories.base import default_project_id
    from app.db import reader

    if not project_id:
        with reader() as c:
            project_id = default_project_id(c)

    data = get_library_service().build(
        project_id,
        include_gaps=include_gaps,
        max_gaps=max_gaps,
        min_gap_citers=min_gap_citers,
    )
    return data


@router.post("/library/build")
async def library_graph_build(
    project_id: str | None = None,
    include_gaps: bool = True,
    max_gaps: int = Query(8, ge=0, le=30),
    min_gap_citers: int = Query(2, ge=1, le=10),
    persist: bool = False,
):
    """
    与 GET 同样的结果，区别是把匹配结果写回 `paper_refs.matched_paper_id`。
    给回填/评测用；前端日常刷新走 GET，避免每次打开页面都写库。
    """
    from app.repositories.base import default_project_id
    from app.db import reader

    if not project_id:
        with reader() as c:
            project_id = default_project_id(c)

    return get_library_service().build(
        project_id,
        include_gaps=include_gaps,
        max_gaps=max_gaps,
        min_gap_citers=min_gap_citers,
        persist_matches=persist,
    )


@router.post("/refs/rebuild")
async def rebuild_refs(
    project_id: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    max_pages: int = Query(60, ge=5, le=400),
):
    """
    从已存的 PDF 重抽参考文献并重建正文引用位置。

    不重跑整个解析：正文 chunk / 向量 / 指纹都不动，只替换 `paper_refs` 与
    `citations` 里属于该版本的行。`dry_run=true` 只返回重抽结果的前几条，便于先看一眼。
    """
    from app.repositories.base import default_project_id
    from app.db import reader

    if not project_id:
        with reader() as c:
            project_id = default_project_id(c)

    try:
        return rebuild_references(
            project_id, force=force, dry_run=dry_run, max_pages=max_pages
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"参考文献回填失败：{exc}") from exc


# ---------------------------------------------------------------- 外部引用网络


@router.post("/build", response_model=GraphResponse)
async def build_graph(req: GraphRequest):
    """
    验收标准：
    1. 接收 seed_arxiv_id、depth、filter_perfunctory；
    2. 实时从 S2 获取引用关系并计算 influence_score / edge weight；
    3. 返回节点 source_url（可点跳原文）与过滤后的 edges。
    """
    if not req.seed_arxiv_id:
        raise HTTPException(status_code=400, detail="seed_arxiv_id 不能为空")
    service = GraphService()
    try:
        data = service.build_graph(
            req.seed_arxiv_id,
            depth=req.depth,
            filter_perfunctory=req.filter_perfunctory,
        )
    except Exception as e:
        # Semantic Scholar 无 key 时易被 429 限流，给出友好提示而非 500
        msg = str(e)
        if "429" in msg or "rate" in msg.lower():
            raise HTTPException(
                status_code=429,
                detail="Semantic Scholar 接口限流，请稍后重试；建议配置 S2_API_KEY 提升配额",
            )
        raise HTTPException(status_code=502, detail=f"图谱构建失败：{msg}")
    nodes = [GraphNode(**n) for n in data["nodes"]]
    edges = [GraphEdge(**e) for e in data["edges"]]
    return GraphResponse(nodes=nodes, edges=edges)


@router.get("/{arxiv_id}/high-impact")
async def high_impact_citations(arxiv_id: str, top_n: int = 10):
    """返回对指定论文影响力最高的 top_n 引用。"""
    service = GraphService()
    return {"arxiv_id": arxiv_id, "citations": service.get_high_impact_citations(arxiv_id, top_n=top_n)}
