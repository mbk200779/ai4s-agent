"""
引用图谱接口：基于 S2/OpenAlex 构建引用网络，计算引用含金量，节点可点跳原文。
"""
from fastapi import APIRouter, HTTPException
from app.schemas.graph import GraphRequest, GraphResponse, GraphNode, GraphEdge
from app.services.graph_service import GraphService

router = APIRouter(prefix="/graph", tags=["图谱"])


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
    data = service.build_graph(
        req.seed_arxiv_id,
        depth=req.depth,
        filter_perfunctory=req.filter_perfunctory,
    )
    nodes = [GraphNode(**n) for n in data["nodes"]]
    edges = [GraphEdge(**e) for e in data["edges"]]
    return GraphResponse(nodes=nodes, edges=edges)


@router.get("/{arxiv_id}/high-impact")
async def high_impact_citations(arxiv_id: str, top_n: int = 10):
    """返回对指定论文影响力最高的 top_n 引用。"""
    service = GraphService()
    return {"arxiv_id": arxiv_id, "citations": service.get_high_impact_citations(arxiv_id, top_n=top_n)}
