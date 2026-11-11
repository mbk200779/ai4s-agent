"""
相似度：把两段正文 + 两条论文元数据算成一组可解释的数。

这里刻意**只算数，不下结论**。关系类型是 `relation.py` 的事——
把两者混在一起是这类系统最常见的错误：阈值调一次，判定逻辑跟着变，
最后没人说得清"为什么这一对被判成重复"。

所有数值都能追溯到某一条可复现的计算：
    body_overlap_new  ← MinHash 草图重合率（新文档视角）
    para_hit_new      ← 段落 simhash 一对一匹配数
    title_sim         ← 标题 token 集合的 Jaccard + 包含关系
    author_sim        ← 作者姓氏集合的 Jaccard
    abstract_cos      ← e5 向量余弦
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.services.dedupe.fingerprint import (
    PARA_HAMMING_MAX,
    best_match_ratio,
    containment,
    cosine,
    hamming,
    jaccard_from_sketch,
    sim_from_hamming,
    tokens,
)

# 标题/作者这类元数据缺失时（本地 PDF 常见）不能当"不相似"处理，
# 否则元数据一空，全部落到 possible_duplicate。这个哨兵值表达"无信息"。
UNKNOWN = -1.0


def _loads(value: Any, default):
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


@dataclass
class PeerFingerprint:
    """一条已入库版本的指纹（可能来自 pdf，也可能来自老数据回填的 chunks）。"""

    version_id: str
    paper_id: str
    paper_title: str
    version_label: str
    project_id: str = ""
    year: int | None = None
    authors: list[str] = field(default_factory=list)
    simhash: str = ""
    minhash: list[str] = field(default_factory=list)
    shingle_count: int = 0
    para_hashes: list[str] = field(default_factory=list)
    abstract_emb: list[float] | None = None
    lead_emb: list[float] | None = None
    abstract_head: str = ""
    lead_head: str = ""
    body_chars: int = 0
    section_count: int = 0
    source: str = "pdf"
    page_count: int | None = None
    status: str = ""
    ingested_at: str = ""

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "PeerFingerprint":
        return cls(
            version_id=row.get("version_id") or "",
            paper_id=row.get("paper_id") or "",
            paper_title=row.get("paper_title") or "",
            version_label=row.get("version_label") or "",
            project_id=row.get("project_id") or "",
            year=row.get("year"),
            authors=_loads(row.get("authors"), []),
            simhash=row.get("simhash") or "",
            minhash=_loads(row.get("minhash"), []),
            shingle_count=int(row.get("shingle_count") or 0),
            para_hashes=_loads(row.get("para_hashes"), []),
            abstract_emb=_loads(row.get("abstract_emb"), None),
            lead_emb=_loads(row.get("lead_emb"), None),
            abstract_head=row.get("abstract_head") or "",
            lead_head=row.get("lead_head") or "",
            body_chars=int(row.get("body_chars") or 0),
            section_count=int(row.get("section_count") or 0),
            source=row.get("source") or "pdf",
            page_count=row.get("page_count"),
            status=row.get("status") or "",
            ingested_at=row.get("ingested_at") or "",
        )


def _norm_title(t: str) -> str:
    t = re.sub(r"\.pdf$", "", (t or "").strip(), flags=re.I)
    t = re.sub(r"[^\w\u4e00-\u9fff]+", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def title_similarity(a: str, b: str) -> float | None:
    """
    标题相似度：token 集合 Jaccard 与"包含关系"取大。

    为什么要包含关系：投稿版标题常带副标题（"Attention Is All You Need:
    A Study of ..."），Jaccard 会把它算得很低，但它其实是同一篇。
    """
    na, nb = _norm_title(a), _norm_title(b)
    if len(na) < 4 or len(nb) < 4:
        return None
    ta, tb = set(tokens(na)), set(tokens(nb))
    if not ta or not tb:
        return None
    jac = len(ta & tb) / len(ta | tb)
    contain = len(ta & tb) / min(len(ta), len(tb))
    return max(jac, contain * 0.92)  # 包含关系打个折，避免"标题极短"时虚高


def _surname(name: str) -> str:
    """取姓氏：拉丁名取最后一个词，中文名取首字。"""
    name = (name or "").strip()
    if not name:
        return ""
    if re.search(r"[\u4e00-\u9fff]", name):
        return name[0]
    parts = re.split(r"[\s.]+", name)
    return (parts[-1] if parts else name).lower()


def author_similarity(a: list[str], b: list[str]) -> float | None:
    """作者重合度：姓氏集合的 Jaccard。任一侧为空返回 UNKNOWN（无信息）。"""
    sa = {_surname(x) for x in a or [] if _surname(x)}
    sb = {_surname(x) for x in b or [] if _surname(x)}
    if not sa or not sb:
        return None
    return len(sa & sb) / len(sa | sb)


def _version_num(label: str) -> int | None:
    m = re.search(r"(\d+)", label or "")
    return int(m.group(1)) if m else None


@dataclass
class Similarity:
    # 正文
    body_overlap_new: float = 0.0      # 新文档的 shingle 有多少能在库内版本找到
    body_overlap_peer: float = 0.0     # 反过来
    jaccard: float = 0.0
    simhash_sim: float = 0.0
    simhash_distance: int = 64
    # 段落
    para_total_new: int = 0
    para_total_peer: int = 0
    para_hit_new: int = 0
    para_hit_peer: int = 0
    para_ratio_new: float = 0.0
    para_ratio_peer: float = 0.0
    # 语义
    abstract_cos: float | None = None
    lead_cos: float | None = None
    # 元数据
    title_sim: float | None = None
    author_sim: float | None = None
    # 规模
    chars_new: int = 0
    chars_peer: int = 0
    len_ratio: float = 1.0
    # 版本
    version_new: int | None = None
    version_peer: int | None = None
    direction: str = "unknown"          # newer | older | same | unknown

    def to_dict(self) -> dict[str, Any]:
        d = {
            "body_overlap_new": round(self.body_overlap_new, 4),
            "body_overlap_peer": round(self.body_overlap_peer, 4),
            "jaccard": round(self.jaccard, 4),
            "simhash_sim": round(self.simhash_sim, 4),
            "simhash_distance": self.simhash_distance,
            "para_total_new": self.para_total_new,
            "para_total_peer": self.para_total_peer,
            "para_hit_new": self.para_hit_new,
            "para_hit_peer": self.para_hit_peer,
            "para_ratio_new": round(self.para_ratio_new, 4),
            "para_ratio_peer": round(self.para_ratio_peer, 4),
            "abstract_cos": None if self.abstract_cos is None else round(self.abstract_cos, 4),
            "lead_cos": None if self.lead_cos is None else round(self.lead_cos, 4),
            "title_sim": None if self.title_sim is None else round(self.title_sim, 4),
            "author_sim": None if self.author_sim is None else round(self.author_sim, 4),
            "chars_new": self.chars_new,
            "chars_peer": self.chars_peer,
            "len_ratio": round(self.len_ratio, 4),
            "version_new": self.version_new,
            "version_peer": self.version_peer,
            "direction": self.direction,
        }
        return d


def compare(new_fp, peer: PeerFingerprint, *, new_title: str = "", new_authors: list[str] | None = None, new_label: str = "") -> Similarity:
    """算一组相似度。`new_fp` 是 fingerprint.Fingerprint（上传时的临时指纹）。"""
    jac = jaccard_from_sketch(
        new_fp.minhash, peer.minhash, na=new_fp.shingle_count, nb=peer.shingle_count
    )
    ov_new = containment(jac, new_fp.shingle_count, peer.shingle_count, direction="a") or 0.0
    ov_peer = containment(jac, new_fp.shingle_count, peer.shingle_count, direction="b") or 0.0

    hit_new, ratio_new = best_match_ratio(new_fp.para_hashes, peer.para_hashes)
    hit_peer, ratio_peer = best_match_ratio(peer.para_hashes, new_fp.para_hashes)

    dist = hamming(new_fp.simhash, peer.simhash)
    vn, vp = _version_num(new_label), _version_num(peer.version_label)
    if vn is not None and vp is not None:
        direction = "same" if vn == vp else ("newer" if vn > vp else "older")
    else:
        direction = "unknown"

    sim = Similarity(
        body_overlap_new=ov_new,
        body_overlap_peer=ov_peer,
        jaccard=jac or 0.0,
        simhash_sim=sim_from_hamming(dist),
        simhash_distance=dist,
        para_total_new=len(new_fp.para_hashes),
        para_total_peer=len(peer.para_hashes),
        para_hit_new=hit_new,
        para_hit_peer=hit_peer,
        para_ratio_new=ratio_new,
        para_ratio_peer=ratio_peer,
        abstract_cos=cosine(new_fp.abstract_emb, peer.abstract_emb),
        lead_cos=cosine(new_fp.lead_emb, peer.lead_emb),
        title_sim=title_similarity(new_title or new_fp.title, peer.paper_title),
        author_sim=author_similarity(new_authors if new_authors is not None else new_fp.authors, peer.authors),
        chars_new=new_fp.body_chars,
        chars_peer=peer.body_chars,
        len_ratio=(new_fp.body_chars / peer.body_chars) if peer.body_chars else 1.0,
        version_new=vn,
        version_peer=vp,
        direction=direction,
    )
    return sim


def prefilter(new_fp, peer: PeerFingerprint) -> bool:
    """
    便宜的先筛：明显不是同一篇的直接跳过，不进后面的段落逐对比较。

    段落比较是 O(n·m)（200×200 = 4 万次汉明），10 篇候选就要 40 万次；
    用草图重合率做闸门可以先砍掉绝大多数。
    """
    if not peer.minhash or not new_fp.minhash:
        return False
    jac = jaccard_from_sketch(
        new_fp.minhash, peer.minhash, na=new_fp.shingle_count, nb=peer.shingle_count
    )
    if jac is None:
        return False
    ov = max(
        containment(jac, new_fp.shingle_count, peer.shingle_count, direction="a") or 0.0,
        containment(jac, new_fp.shingle_count, peer.shingle_count, direction="b") or 0.0,
    )
    if ov >= 0.10:
        return True
    # 正文不重合也可能"同主题相关"：摘要向量足够近才值得细看
    if new_fp.abstract_emb and peer.abstract_emb:
        c = cosine(new_fp.abstract_emb, peer.abstract_emb)
        if c is not None and c >= 0.72:
            return True
    return False
