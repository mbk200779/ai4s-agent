"""
去重接口：上传前预检、记录选择、回看历史、版本差异。

预检是**只读**的：读 PDF 算指纹、入库零写入（连临时文件都立刻删）。
真正的入库仍然走 `/papers/upload`，由它带上用户选的动作。
把两者分开，是为了让"要不要入库"这个决定可以与"入库"这件事解耦：
用户在弹窗里犹豫、改主意、关掉页面，都不会在库里留下半条记录。
"""
from __future__ import annotations

import hashlib
import os
import tempfile

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.repositories import PaperVersionRepository, ProjectRepository
from app.schemas.dedupe import (
    DedupeBackfillResult,
    DedupeDecideRequest,
    DedupeDecideResponse,
    DedupeDecisionList,
    DedupeDecisionRow,
)
from app.services.dedupe.service import get_service

router = APIRouter(prefix="/dedupe", tags=["去重判定"])


def _project(project_id: str | None):
    if project_id:
        p = ProjectRepository.get(project_id)
        if not p:
            raise HTTPException(status_code=404, detail=f"课题不存在: {project_id}")
        return p
    return ProjectRepository.get_or_create_default()


@router.post("/precheck")
async def precheck(
    file: UploadFile = File(...),
    project_id: str | None = Form(None),
    version: str = Form(""),
    title: str | None = Form(None),
    include_other_projects: bool = Form(False),
    with_diff: bool = Form(True),
):
    """
    上传前预检：算指纹、与库内版本比对、给出关系与建议动作。

    注意 `title` 参数：前端可以传，但**默认不传**。本地上传的标题默认是文件名，
    拿文件名参与判定就等于让"命名"决定结论；只有 PDF 里抽不出标题时才用得上它。
    """
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="仅支持 PDF 文件")

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    path = tmp.name
    try:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            tmp.write(chunk)
        tmp.close()
        project = _project(project_id)
        return get_service().precheck(
            path,
            project_id=project.id,
            version_label=version or "",
            title_hint=title or "",
            include_other_projects=include_other_projects,
            with_diff=with_diff,
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@router.post("/decide", response_model=DedupeDecideResponse)
async def decide(req: DedupeDecideRequest):
    """记录用户的选择。同一个 pair_key 再次提交会覆盖上一条（用户改主意）。"""
    if req.relation not in ("version_of", "possible_duplicate", "related"):
        raise HTTPException(status_code=400, detail=f"未知关系类型: {req.relation}")
    if req.action not in ("keep_new", "keep_both", "link_only"):
        raise HTTPException(status_code=400, detail=f"未知操作: {req.action}")
    project = _project(req.project_id)
    if req.peer_version_id and not PaperVersionRepository.get(req.peer_version_id):
        raise HTTPException(status_code=404, detail="peer_version_id 不存在")
    res = get_service().decide(
        project_id=project.id,
        relation=req.relation,
        action=req.action,
        confidence=req.confidence,
        evidence={
            **req.evidence,
            "new_label": req.new_label,
            "peer_label": req.peer_label,
            "peer_version_id": req.peer_version_id,
        },
        new_simhash=req.new_simhash,
        peer_simhash=req.peer_simhash,
        new_version_id=req.new_version_id,
        peer_version_id=req.peer_version_id,
        new_label=req.new_label,
        peer_label=req.peer_label,
        suppress=req.suppress,
    )
    note = (
        "已记住：下次遇到同一对内容不再询问"
        if req.suppress
        else "已记录，下次仍会提示"
    )
    return DedupeDecideResponse(**res, remembered_note=note)


@router.get("/decisions", response_model=DedupeDecisionList)
async def decisions(project_id: str | None = None, limit: int = 20):
    project = _project(project_id)
    rows = get_service().decisions(project.id, limit=limit)
    return DedupeDecisionList(
        rows=[DedupeDecisionRow(**{k: v for k, v in r.items() if k in DedupeDecisionRow.model_fields}) for r in rows],
        total=len(rows),
    )


@router.get("/versions/{base_version_id}/diff/{target_version_id}")
async def version_diff(base_version_id: str, target_version_id: str):
    """两个已解析版本之间的差异摘要（段落配对 + 章节增删 + 数字修订）。"""
    d = get_service().version_diff(base_version_id, target_version_id)
    if not d:
        raise HTTPException(status_code=404, detail="版本不存在或尚未解析出正文")
    return d


@router.post("/backfill", response_model=DedupeBackfillResult)
async def backfill(project_id: str | None = None, force: bool = False):
    """
    为已入库的老版本补算指纹。

    没有指纹的老论文不会参与比对——不是"被跳过"，而是"看不见"。
    所以升级后跑一次这个接口，历史库才真正纳入去重范围。
    """
    project = _project(project_id)
    from app.db import reader

    with reader() as c:
        rows = c.execute(
            """SELECT pv.id FROM paper_versions pv
               JOIN papers p ON p.id = pv.paper_id
               WHERE p.project_id = ? AND pv.status = 'indexed'""",
            (project.id,),
        ).fetchall()
    svc = get_service()
    result = {"scanned": len(rows), "indexed": 0, "skipped": 0, "failed": 0, "versions": []}
    for r in rows:
        brief = svc.index_version(r["id"], force=force)
        if brief:
            result["indexed"] += 1
            result["versions"].append(
                {"version_id": r["id"], "simhash": brief["simhash"], "para_count": brief["para_count"]}
            )
        else:
            result["skipped"] += 1
    return DedupeBackfillResult(**result)
