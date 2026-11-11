"""
库外推荐接口：空检索时按问题去 arXiv 找几篇能补进库的论文。

为什么单独一个前缀 `/recommend`，不塞进 `/papers`：
`/papers` 那组管的都是"库内的东西"（上传、导入、检索已入库的片段），
而这里的每一条结果**都还不在库内**，它回答的是"要不要把它收进来"。
两者的失败模式也不同：那边的 404 是内容缺失，这边的空结果是正常业务状态
（arXiv 就是没有），混在一个前缀下客户端得靠猜。

为什么端点是同步 `def`：函数体里是一次 1~2 s 的外部 HTTP 请求，
写成 `async def` 会把事件循环整个按住，期间所有接口一起卡。
FastAPI 会把同步端点丢进线程池，这才是它该待的地方。
"""
from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services import external_search

router = APIRouter(prefix="/recommend", tags=["库外推荐"])


class RecommendRequest(BaseModel):
    question: str = Field(..., description="用户原始问题（中文即可）")
    search_text: str | None = Field(
        default=None,
        description="问答链路已经翻好的英文检索文本，传了就不再翻译",
    )
    limit: int = Field(default=5, ge=1, le=10, description="最多推荐几篇")
    force: bool = Field(default=False, description="true = 跳过 10 分钟缓存，重新打 arXiv")


@router.post("/papers")
def recommend_papers(req: RecommendRequest):
    """
    按问题在 arXiv 检索候选论文。

    返回 `status` 而不是靠 HTTP 状态码区分业务结果：
    `ok` / `no_result` / `no_query` 都是**正常业务状态**（arXiv 没有、问句没英文词），
    前端要展示的是不同的说明文字，不是错误页。
    只有 arXiv 本身不可用（网络、超时、上游 5xx）才会返回 `status=error`，
    这时前端给的是「重试」，而不是「换一个说法」。
    """
    return external_search.recommend(
        question=req.question,
        search_text=req.search_text,
        limit=req.limit,
        force=req.force,
    )
