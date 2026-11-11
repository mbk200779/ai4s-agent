"""
去重编排：上传前预检 → 关系判定 → 用户选择 → 记录。

调用顺序（也是数据流）：
    1. `precheck()`   读 PDF 算指纹（不落库、不解析），与库内版本比，出关系与建议
    2. 用户看提示点一个操作，或系统按上次记下的选择自动套用
    3. `decide()`     写判定记录（按 pair_key，同一对内容只留一条）
    4. 入库完成后 `index_version()` 落指纹，供下一次上传比对

"上传前"而不是"入库后"判定，是因为入库要几十秒（解析 + 向量化），
等它跑完再告诉用户"这条是重复的"，用户已经等了一分钟——那时候再让他选
"要不要保留"，等于让他白等。真正贵的是解析，所以判定必须挡在解析前面。
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

from app.services.dedupe import diff as diff_mod
from app.services.dedupe import store
from app.services.dedupe.fingerprint import Fingerprint, from_chunks, from_pdf
from app.services.dedupe.relation import (
    DISTINCT,
    RELATION_LABEL,
    classify,
    suggest_actions,
    version_hint,
)
from app.services.dedupe.similarity import PeerFingerprint, compare, prefilter
from app.services.dedupe.similarity import _version_num

# 一次上传最多展示 3 条候选关系：再多用户不会看，只会随手点一个。
MAX_CANDIDATES = 3


def _sha256(path: str, chunk: int = 1 << 20) -> str:
    """流式算文件哈希：80MB 的教材整份读进内存没必要，口径要与上传时一致。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


class DedupeService:
    # ---------- 预检 ----------
    def precheck(
        self,
        pdf_path: str,
        *,
        project_id: str,
        version_label: str = "",
        title_hint: str = "",
        authors_hint: list[str] | None = None,
        include_other_projects: bool = False,
        with_diff: bool = True,
    ) -> dict[str, Any]:
        """
        返回一条"要不要问用户"的判定结果。

        `title_hint` 只在 PDF 里抽不到标题时兜底；**文件名不会走到这里**——
        路由层传的是 None，除非调用方明确给了标题。
        """
        t0 = time.time()

        # 0) 字节级完全相同：这是唯一不需要"判定"的情况。
        #    同一份文件再传一次，任何关系判断都是多余的——直接告诉前端别问了。
        #    放在算指纹之前，是因为这条路只要几十毫秒，而算指纹要 1~2 秒。
        exact = store.find_by_file_hash(project_id, _sha256(pdf_path))
        if exact:
            return {
                "ask": False,
                "suppressed": False,
                "primary": None,
                "others": [],
                "exact_duplicate": exact,
                "fingerprint": {},
                "elapsed": round(time.time() - t0, 2),
                "version_label": version_label,
            }

        fp = from_pdf(pdf_path)
        if title_hint:
            fp.title = fp.title or title_hint
        if authors_hint:
            fp.authors = fp.authors or authors_hint

        peers = store.list_peers(
            project_id, include_other_projects=include_other_projects
        )
        candidates: list[dict[str, Any]] = []
        for peer in peers:
            if not prefilter(fp, peer):
                continue
            sim = compare(
                fp,
                peer,
                new_title=fp.title,
                new_authors=fp.authors,
                new_label=version_label,
            )
            verdict = classify(sim)
            if verdict.relation == DISTINCT:
                continue
            key = store.pair_key(fp.simhash, peer.simhash)
            remembered = store.get_decision(project_id, key)
            cand = {
                "version_id": peer.version_id,
                # peer 侧 simhash 要一起给前端：判定记录的 pair_key 是这两个值的
                # 无序对，前端记录选择时必须原样带回来。让前端自己算一遍等于
                # 把"键怎么拼"的规则复制到两个地方。
                "peer_simhash": peer.simhash,
                "paper_id": peer.paper_id,
                "paper_title": peer.paper_title,
                "version_label": peer.version_label,
                "year": peer.year,
                "authors": peer.authors[:8],
                "pages": peer.page_count,
                "other_project": peer.project_id != project_id,
                "relation": verdict.relation,
                "relation_label": RELATION_LABEL.get(verdict.relation, verdict.relation),
                "kind": verdict.kind,
                "confidence": round(verdict.confidence, 4),
                "reasons": verdict.reasons,
                "caveats": verdict.caveats,
                "similarity": sim.to_dict(),
                "hint": version_hint(sim, version_label, peer.version_label),
                "actions": suggest_actions(verdict.relation),
                "memorized": (
                    {
                        "action": remembered["action"],
                        "relation": remembered["relation"],
                        "decided_at": remembered["created_at"],
                        "suppress": bool(remembered["suppress"]),
                    }
                    if remembered
                    else None
                ),
                "fingerprint_source": peer.source,
            }
            if with_diff and verdict.relation in ("version_of", "possible_duplicate"):
                cand["diff"] = self._diff_against_peer(fp, peer, version_label)
            candidates.append(cand)

        candidates.sort(key=lambda c: (c["relation"] != "version_of", -c["confidence"]))
        candidates = candidates[:MAX_CANDIDATES]
        primary = candidates[0] if candidates else None

        # 这一对以前定过、并且勾了"不再提示" → 不问，直接按老选择。
        # 注意这里找的是**任意一个**候选的旧记录，不只是排第一的那个：
        # "不再提示"的语义是"这对内容我定过了"，跟这次谁排第一无关。
        # 只认 primary 会出现一个很别扭的情况——库里已经存了同一份内容，
        # 重新上传时 primary 变成"新内容 vs 刚入库的那份"，于是系统又把
        # 已经答过的问题问了一遍。
        memorized_hit = next(
            (c for c in candidates if c["memorized"] and c["memorized"].get("suppress")),
            None,
        )
        if memorized_hit is not None:
            primary = memorized_hit
            candidates = [memorized_hit] + [c for c in candidates if c is not memorized_hit]
        suppressed = memorized_hit is not None
        return {
            "ask": bool(primary) and not suppressed,
            "suppressed": suppressed,
            "primary": primary,
            "others": candidates[1:],
            "fingerprint": {
                **fp.brief(),
                "simhash": fp.simhash,
                "candidates_considered": len(candidates),
                "peers_scanned": len(peers),
            },
            "elapsed": round(time.time() - t0, 2),
            "version_label": version_label,
        }

    def _diff_against_peer(self, fp: Fingerprint, peer: PeerFingerprint, new_label: str) -> dict[str, Any] | None:
        """
        差异摘要：库内版本用 chunk（已解析），本次用 PDF 文本。

        方向按版本号排：v1 → v7 读起来是"从旧到新"，反着写会让人以为是回退。
        版本号比不出来时（比如没声明的）就按"库内 → 本次"。
        """
        from app.repositories.chunk_repo import ChunkRepository

        try:
            peer_chunks = ChunkRepository.list_by_version(peer.version_id)
        except Exception:
            peer_chunks = []
        if not peer_chunks or not fp.text:
            return None
        peer_label = peer.version_label or "库内版本"
        new_label = new_label or "本次上传"
        vn, vp = _version_num(new_label), _version_num(peer_label)
        try:
            if vn is not None and vp is not None and vn < vp:
                # 本次更早：把本次放左边，读作 "v1 → v7"
                return diff_mod.diff_versions(
                    from_label=new_label,
                    from_chunks=_TextChunks(fp.text),
                    from_pages=None,
                    from_sections=fp.sections,
                    to_label=peer_label,
                    to_chunks=peer_chunks,
                    to_pages=peer.page_count,
                )
            return diff_mod.diff_versions(
                from_label=peer_label,
                from_chunks=peer_chunks,
                from_pages=peer.page_count,
                to_label=new_label,
                to_chunks=_TextChunks(fp.text),
                to_pages=None,
                to_sections=fp.sections,
            )
        except Exception:
            return None

    # ---------- 记录 ----------
    def decide(
        self,
        *,
        project_id: str,
        relation: str,
        action: str,
        confidence: float,
        evidence: dict[str, Any],
        new_simhash: str,
        peer_simhash: str,
        new_version_id: str = "",
        peer_version_id: str = "",
        new_label: str = "",
        peer_label: str = "",
        suppress: bool = True,
        decided_by: str = "user",
    ) -> dict[str, Any]:
        key = store.pair_key(new_simhash, peer_simhash)
        did = store.save_decision(
            project_id=project_id,
            key=key,
            relation=relation,
            action=action,
            confidence=confidence,
            evidence=evidence,
            new_simhash=new_simhash,
            peer_simhash=peer_simhash,
            new_version_id=new_version_id,
            peer_version_id=peer_version_id,
            new_label=new_label,
            peer_label=peer_label,
            suppress=suppress,
            decided_by=decided_by,
        )
        return {"decision_id": did, "pair_key": key}

    def decisions(self, project_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = store.list_decisions(project_id, limit=limit)
        for r in rows:
            r["relation_label"] = RELATION_LABEL.get(r.get("relation", ""), r.get("relation", ""))
        return rows

    # ---------- 入库后落指纹 ----------
    def index_version(self, version_id: str, *, force: bool = False) -> dict[str, Any] | None:
        """
        解析完成后为版本落指纹。失败不抛：指纹是"下次上传才有用"的副产品，
        不该让一次入库因为算指纹失败而回滚。
        """
        if not force and store.get_fingerprint(version_id):
            return None
        try:
            from app.repositories import PaperRepository
            from app.repositories.chunk_repo import ChunkRepository

            chunks = ChunkRepository.list_by_version(version_id)
            if not chunks:
                return None
            version = PaperVersionLookup.get(version_id)
            paper = PaperRepository.get(version.paper_id) if version else None
            fp = from_chunks(
                chunks,
                title=getattr(paper, "title", "") or "",
                authors=getattr(paper, "authors", []) or [],
            )
            store.save_fingerprint(version_id, fp.to_row())
            return fp.brief()
        except Exception:
            return None

    def version_diff(self, base_version_id: str, target_version_id: str) -> dict[str, Any] | None:
        from app.repositories import PaperVersionRepository
        from app.repositories.chunk_repo import ChunkRepository

        a = PaperVersionRepository.get(base_version_id)
        b = PaperVersionRepository.get(target_version_id)
        if not a or not b:
            return None
        ca = ChunkRepository.list_by_version(base_version_id)
        cb = ChunkRepository.list_by_version(target_version_id)
        if not ca or not cb:
            return None
        return diff_mod.diff_versions(
            from_label=a.version_label,
            from_chunks=ca,
            from_pages=a.page_count,
            to_label=b.version_label,
            to_chunks=cb,
            to_pages=b.page_count,
        )


class _TextChunks:
    """把纯文本伪装成 chunk 列表，好复用 diff 的实现（不需要 section_path）。"""

    def __init__(self, text: str):
        from app.services.dedupe.fingerprint import normalize, split_paragraphs

        self._items = [
            type("_C", (), {"text": p, "section_path": None})()
            for p in split_paragraphs(normalize(text))
        ]

    def __iter__(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)

    def __getitem__(self, i):
        return self._items[i]


class PaperVersionLookup:
    """延迟导入，避免 service 顶层依赖 repo 造成循环引用。"""

    @staticmethod
    def get(version_id: str):
        from app.repositories import PaperVersionRepository

        return PaperVersionRepository.get(version_id)


def get_service() -> DedupeService:
    return DedupeService()
