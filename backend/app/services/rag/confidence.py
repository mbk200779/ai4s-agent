"""
分级置信：把「能答 / 不能答」换成三档，并且把"缺什么"说清楚。

为什么二值拒答在科研场景里是错的：
科研用户读一段带引用的答案时，脑子里的判断从来不是二元。他会分三种情况处理——
1. 原文直接写着，我信，可以直接引用（verified）；
2. 原文有相关证据，但结论是我（或模型）推出来的，得回去核一眼（needs_check）；
3. 原文里根本没有，这句话不能用（unsupported）。
把 2 折叠进 1 是幻觉的来源；把 2 折叠进 3 则是把本来可用的信息扔掉。
所以三档不是"更细的 UI"，而是**把推理链上本来就存在的三种状态显式化**。

三条硬约束的落点：
- **不静默降级**：分档结果写在响应体（`confidence` / `claim.confidence`）里，
  provenance 里也存一份；改阈值改的是配置，响应里带回 `version + fingerprint`，
  复盘时能确认"当时用的是哪套线"，而不是靠翻 git。
- **只在"无任何检索命中"时完整拒答**：`unsupported` 但检索到了片段时，
  照样把答案吐出来并标注证据不足——拒答与否是"有没有材料"的问题，
  不是"结论站不站得住"的问题，两者混在一起会让用户分不清是系统坏了还是库里没有。
- **needs_check 必须说明缺什么**：只说"需核对"等于把判断责任推回给用户而不给线索。
  这里固定输出 `missing` 列表（缺表格 / 缺对照实验 / 仅单篇支持 / 跨段推断 …），
  每条都有 code 与中文说明，前端可以逐条展示。

分档只用检索与锚点阶段已有的信号（证据句重合度、锚点精度、片段类型、论文数、
检索融合分），不额外调模型：多一次模型调用会让"置信"本身变成不可复现的东西。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Sequence

from app.core.config import settings

VERIFIED = "verified"
NEEDS_CHECK = "needs_check"
UNSUPPORTED = "unsupported"

LEVELS = (VERIFIED, NEEDS_CHECK, UNSUPPORTED)

LABELS = {
    VERIFIED: "已验证",
    NEEDS_CHECK: "需核对",
    UNSUPPORTED: "证据不足",
}

# 一句话说明：前端直接显示，不在前端再写一份文案（两份文案必然漂移）
DESCRIPTIONS = {
    VERIFIED: "原文有直接证据，可据此判定",
    NEEDS_CHECK: "有相关证据，但需人工核对后才能采信",
    UNSUPPORTED: "没有找到能支撑这句话的原文",
}

# 「缺什么」的话术表。code 是稳定标识（前端可据此上图标/筛选），text 是人话。
# 文案里允许出现占位符，由 _fill() 填，避免每个调用点各拼一套字符串。
MISSING_TEXT = {
    "no_citation": "这句话没有引用标注",
    "no_match": "引用的片段里找不到能支撑这句话的句子",
    "weak_match": "证据句与原话的重合度只有 {score:.2f}，低于验证线 {threshold:.2f}",
    "no_table": "数值 {numbers} 不在证据原句中，缺表格里的原始数据",
    "no_experiment": "结论含比较表述，但证据里没有对照实验或基线数值",
    # 这条专治"换了对象但句式没变"：问 TPU、原文写 GPU，语义模型会给 0.7+，
    # 字面分也不低（8 块、训练，全都对得上）。只有专有名词能把它抓出来。
    "entity_mismatch": "结论中的 {terms} 未在证据原句中出现，可能换了对象",
    "cross_section": "证据由 {chunks} 处片段拼接而成，属于推断而非直引",
    "single_paper": "只有 {papers} 篇论文支持，跨文献结论需再核一篇",
    "ocr_precision": "定位来自 OCR，识别文本可能有个别错字",
    "weak_retrieval": "检索融合分 {top:.3f} 低于阈值 {threshold:.3f}，片段可能不是最佳出处",
}

# 聚合展示用的无参文案：句子级那条带着具体分数（"只有 0.50"），
# 汇总到 30 题的表里会变成"看起来所有句子都是 0.50"。聚合只保留定性表述。
MISSING_AGG = {
    "weak_match": "证据句与原话重合度低于验证线",
    "no_table": "结论中的数值不在证据原句中，缺表格原始数据",
    "cross_section": "证据由多处片段拼接而成，属于推断而非直引",
    "single_paper": "仅单篇论文支持，跨文献结论需再核一篇",
    "entity_mismatch": "结论中的专有名词未在引用片段中出现",
    "weak_retrieval": "检索融合分低于阈值，片段可能不是最佳出处",
}

# 展示顺序：越靠前越"致命"，前端只显示前三条也不会漏掉关键信息
PRIORITY = [
    "no_citation", "no_match", "weak_match", "no_table", "entity_mismatch",
    "no_experiment", "cross_section", "single_paper", "ocr_precision",
    "weak_retrieval",
]
_ORDER = {code: i for i, code in enumerate(PRIORITY)}

_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")
# 专有名词/缩写：GPU、TPU、MLM、ImageNet 这类。只抓全大写 2~6 位，
# 不会误抓句首大写的普通词（"The" 是大写开头但后面是小写）。
_CAPS_RE = re.compile(r"\b[A-Z][A-Za-z0-9]{1,5}\b")
# 比较/效果类表述：出现它就意味着这句话在断言"谁比谁好"，
# 这类结论必须有对照数据，否则只能算"作者这么说"，不能算"实验证明"。
_CMP_RE = re.compile(
    r"优于|劣于|高于|低于|超过|优于|相比|对比|提升|降低|减少|增加|下降|持平|领先|"
    r"outperform|improv|better|worse|higher|lower|reduce|increase|compare|versus|"
    r"relative to|gain",
    re.I,
)
# 推断连接词：句子里出现它就说明这句是"由前文推出来的"，不是原文直陈。
_INFER_RE = re.compile(
    r"因此|所以|由此|也就是说|这说明|意味着|可见|从而|综上|可见得|表明|"
    r"thus|therefore|hence|indicating|suggests?|implies?|we can (?:see|infer)",
    re.I,
)

# 只有这些精度能称为"落到句子"。block/line 是"知道大概在哪一块"，
# ocr 是"知道在哪，但字可能是错的"——两者都不能支撑"已验证"。
_TIGHT_PRECISIONS = {"sentence", "cell"}


def _fill(code: str, **kw: Any) -> str:
    return MISSING_TEXT[code].format(**kw)


def thresholds() -> dict[str, Any]:
    """当前生效的三档阈值。响应里原样回传，便于对照复现。"""
    return {
        "verified_score": round(float(settings.confidence_verified_score), 4),
        "needs_check_score": round(float(settings.confidence_needs_check_score), 4),
        "cross_paper_min_sources": int(settings.confidence_cross_paper_min_sources),
    }


def config_block() -> dict[str, Any]:
    """
    阈值版本块：`version` 是人给的，`fingerprint` 是从数值算出来的。

    两个都要：version 用来对"我们对外宣称的配置"，fingerprint 用来抓
    "有人改了 .env 但忘了 bump version"——这种情况下 fingerprint 会变，
    日志里立刻能看出来，不至于把两套线的结果混在一起比较。
    """
    th = thresholds()
    raw = json.dumps(th, sort_keys=True)
    return {
        "version": settings.confidence_version,
        "prompt_version": settings.prompt_version,
        "fingerprint": hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10],
        "thresholds": th,
        "labels": dict(LABELS),
        "descriptions": dict(DESCRIPTIONS),
    }


def caps_terms(text: str) -> list[str]:
    """专有名词/缩写（GPU、TPU、MLM…）。给"换了对象"这条判据用，也供 evidence 锚定。"""
    return sorted({m.group(0) for m in _CAPS_RE.finditer(text or "") if len(m.group(0)) >= 2})


def _numbers(text: str | None) -> set[str]:
    """抽数值。逗号当千分位去掉：`1,024` 与 `1024` 应该算同一个数。"""
    return {m.group(0).replace(",", "") for m in _NUM_RE.finditer(text or "")}


def _sorted_missing(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda m: _ORDER.get(m["code"], 99))


def grade_claim(
    claim_text: str,
    evidence: Sequence[dict[str, Any]],
    *,
    query_type: str | None = None,
    retrieval_weak: bool = False,
    top_raw_score: float | None = None,
    reject_threshold: float | None = None,
    answer_papers: int | None = None,
    th: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    给一条 claim 定档。返回的结构会被原样塞进 claim 事件与响应体。

    `evidence` 是 `answer._evidence` 的产物（每条角标的校验结果）。
    注意这里判的是**句子级**：一条 claim 可能挂两个角标，只要有一个站得住就够，
    但"站得住"还分深浅——所以取最高分那条作为定档依据，其余作为佐证计数。
    """
    th = th or thresholds()
    text = claim_text or ""
    supported = [e for e in evidence if e.get("status") == "supported"]

    caps = caps_terms(text)
    signals: dict[str, Any] = {
        "evidence": len(evidence),
        "supported": len(supported),
        "best_score": 0.0,
        "papers": 0,
        "precisions": sorted({(e.get("precision") or "none") for e in supported}),
        "kinds": sorted({(e.get("kind") or "none") for e in supported}),
        "numeric_claim": bool(_NUM_RE.search(text)),
        "caps_terms": sorted(caps),
        "comparative": bool(_CMP_RE.search(text)),
        "inferential": bool(_INFER_RE.search(text)),
        "retrieval_weak": bool(retrieval_weak),
    }

    if not evidence:
        return _claim_result(UNSUPPORTED, 0.0, [{"code": "no_citation", "text": _fill("no_citation")}], signals)
    if not supported:
        return _claim_result(UNSUPPORTED, 0.0, [{"code": "no_match", "text": _fill("no_match")}], signals)


    best = max(supported, key=lambda e: float(e.get("match_score") or 0.0))
    best_score = float(best.get("match_score") or 0.0)
    signals["best_score"] = round(best_score, 4)

    # 有效支持度 = max(字面分, 语义支持度)。
    # 为什么取 max 而不是加权平均：两个分各自都有"确定"的情形——
    # 数值/符号对上时字面分接近满分（语义模型反而可能因为句子长而偏低），
    # 中文改写英文原句时字面分接近 0（语义模型能到 0.9）。取平均会让两边都变弱。
    sem_values = [float(e.get("sem_score")) for e in supported if e.get("sem_score") is not None]
    sem_best = max(sem_values) if sem_values else None
    signals["sem_score"] = round(sem_best, 4) if sem_best is not None else None
    effective = max(best_score, sem_best or 0.0)
    signals["effective_score"] = round(effective, 4)

    chunks = {e.get("chunk_id") for e in supported if e.get("chunk_id")}
    papers = {e.get("arxiv_id") or e.get("chunk_id") for e in supported}
    signals["papers"] = len(papers)
    precisions = set(signals["precisions"])
    kinds = set(signals["kinds"])
    ev_text = " ".join((e.get("text") or "") for e in supported)

    missing: list[dict[str, Any]] = []

    # 1) 重合度：不到验证线就是"这句话可能对，但不是原文这句说的"
    if effective < float(th["verified_score"]):
        missing.append({
            "code": "weak_match",
            "text": _fill("weak_match", score=effective, threshold=float(th["verified_score"])),
        })

    # 2) 定位精度：只把 OCR 算进"缺什么"。
    #    块级/行级定位说明的是"高亮只能画到那一段"，不是"证据不够"——
    #    证据句是在 chunk 原文里现场切出来的，仍然是原文。把它算成降级理由，
    #    会让锚点覆盖率低的语料（本库 38%~59%）整体被打成需核对，
    #    verified 这一档就名存实亡。定位精度单独放 `locate`，前端照样能标"块级定位"。
    locate = {"precision": best.get("precision") or None, "note": None}
    if "ocr" in precisions:
        missing.append({"code": "ocr_precision", "text": _fill("ocr_precision")})
    elif not (precisions & _TIGHT_PRECISIONS):
        locate["note"] = "块级定位（该片段未建立句级锚点）"

    # 3) 数值：原句里到底有没有这个数
    #    断言里的数字如果**一个都对不上**，只可能来自两处——表格（没取到）或推断。
    #    这是"缺表格"最可靠的判据，比看 chunk.kind 准：正文里的数值同样是表格转述。
    #    为什么要求"一个都不中"而不是"缺一个就报"：claim 里混杂着型号里的数字
    #    （`ViT-H/14` 的 14）、标准差（`88.55 ± 0.04` 的 0.04），
    #    按"缺一个"判会把正确的直引句全打成缺表格——实测 3 条里错 2 条。
    if signals["numeric_claim"]:
        claim_nums = _numbers(text)
        hit_nums = claim_nums & _numbers(ev_text)
        if claim_nums and not hit_nums and not ({"table"} & kinds or "cell" in precisions):
            missing.append({
                "code": "no_table",
                "text": _fill("no_table", numbers="、".join(sorted(claim_nums)[:3])),
            })

    # 3.5) 专有名词对不上：换对象最典型的症状。
    #     判据是"这段被引用的正文里有没有这个词"，而不是"这一句里有没有"——
    #     证据句常常不重复主语。锚定结果由 _evidence 按整段 chunk 算好带进来。
    if signals["caps_terms"]:
        present = {t for e in supported for t in (e.get("entity_present") or [])}
        lost = [t for t in signals["caps_terms"] if t not in present]
        if lost:
            missing.append({
                "code": "entity_mismatch",
                "text": _fill("entity_mismatch", terms="、".join(lost[:3])),
            })

    # 4) 对照实验：说"更好/提升"就必须有对比数值
    if signals["comparative"] and not _NUM_RE.search(ev_text):
        missing.append({"code": "no_experiment", "text": _fill("no_experiment")})

    # 5) 跨段推断：推断连接词，或证据来自多处片段
    #    对比题天然要多段证据（A 一篇、B 一篇），那不是"推断"，不该记账；
    #    但对比题里出现"因此/说明"，仍然算推断。
    if signals["inferential"] or ((query_type or "") != "compare" and len(chunks) >= 2):
        missing.append({"code": "cross_section", "text": _fill("cross_section", chunks=len(chunks) or 2)})

    # 6) 单篇支持：只有对比/综述类结论才强求多篇——
    #    点查题（"β2 是多少"）本来就只可能出自一篇，要求两篇会把所有点查打成需核对。
    #    注意判的是**整条回答**覆盖了几篇，不是这一句引了几篇：
    #    "ViT 用 JFT-300M，BERT 用 BooksCorpus" 拆成两句后每句只引一篇，
    #    按句判会把正常的对比答成"仅单篇支持"（实测 30 题里误报 11 次）。
    if (query_type or "") in ("compare", "survey"):
        n_papers = answer_papers if answer_papers is not None else len(papers)
        if n_papers < int(th["cross_paper_min_sources"]):
            missing.append({
                "code": "single_paper",
                "text": _fill("single_paper", papers=n_papers or 1),
            })

    # 7) 检索层面偏弱：片段本身可能不是最佳出处，句子对得上也不能算已验证
    if retrieval_weak:
        missing.append({
            "code": "weak_retrieval",
            "text": _fill(
                "weak_retrieval",
                top=float(top_raw_score or 0.0),
                threshold=float(reject_threshold if reject_threshold is not None else th["needs_check_score"]),
            ),
        })

    missing = _sorted_missing(missing)
    level = VERIFIED if not missing else NEEDS_CHECK
    return _claim_result(level, effective, missing, signals, locate)


def _claim_result(
    level: str,
    score: float,
    missing: list[dict[str, Any]],
    signals: dict[str, Any],
    locate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "level": level,
        "label": LABELS[level],
        "score": round(float(score), 4),
        "missing": missing,
        "locate": locate or {"precision": None, "note": None},
        "signals": signals,
    }


def grade_answer(
    claims: Sequence[dict[str, Any]],
    *,
    sources: Sequence[Any] = (),
    query_type: str | None = None,
    top_raw_score: float | None = None,
    reject_threshold: float | None = None,
    retrieval_weak: bool = False,
    rejected: bool = False,
    reject_reason: str | None = None,
    applicable: bool = True,
) -> dict[str, Any]:
    """
    整条回答的档位。规则是"最弱的一句决定整体"：

    - 任一句 unsupported → 整体不可能是 verified（有句子没站住，就不能说"可据此判定"）；
      但只有**全部** unsupported 才降到 unsupported；
    - 任一句 needs_check → 整体 needs_check；
    - 全 verified → verified。

    这条规则偏保守，但方向是对的：用户扫一眼"需核对"就该知道这篇回答里有需要他
    亲自确认的地方，而不是被"已验证"骗过去。
    """
    counts = {"claims": len(claims), "verified": 0, "needs_check": 0, "unsupported": 0}
    codes: dict[str, dict[str, Any]] = {}
    scores: list[float] = []
    block_located = 0

    for c in claims:
        conf = c.get("confidence") or {}
        lvl = conf.get("level")
        if lvl not in LEVELS:
            # 兼容老结构（只有 status 的 claim）：有据即 verified。
            # 老 claim 没有分档信息，把它当 verified 是唯一不产生新噪声的选择。
            lvl = VERIFIED if c.get("status") == "supported" else UNSUPPORTED
        counts[lvl] = counts.get(lvl, 0) + 1
        scores.append(float(conf.get("score") or 0.0) if lvl != UNSUPPORTED else 0.0)
        for m in conf.get("missing") or []:
            slot = codes.setdefault(
                m["code"],
                {"code": m["code"], "text": MISSING_AGG.get(m["code"], m["text"]), "claims": 0},
            )
            slot["claims"] += 1
        if (conf.get("locate") or {}).get("note"):
            block_located += 1

    signals = {
        "sources": len(sources),
        "papers": len({getattr(s, "arxiv_id", None) for s in sources}),
        "top_raw_score": round(float(top_raw_score), 4) if top_raw_score is not None else None,
        "reject_threshold": reject_threshold,
        "retrieval_weak": bool(retrieval_weak),
        "query_type": query_type,
        # 只能块级定位的句子数：不是置信问题，是"点了角标只能框到那一段"，
        # 单独计数，前端提示一句就够，不参与分档。
        "block_located": block_located,
    }
    missing = _sorted_missing(list(codes.values()))

    if not applicable:
        return {
            "applicable": False,
            "level": None,
            "label": "不适用",
            "description": "闲聊/直答，不涉及文献证据",
            "score": 0.0,
            "counts": counts,
            "missing": [],
            "notes": [],
            "signals": signals,
            "config": config_block(),
        }

    notes: list[str] = []
    if rejected:
        level = UNSUPPORTED
        notes.append(reject_reason or "未检索到任何相关片段")
    elif counts["claims"] == 0:
        level = UNSUPPORTED
        notes.append("回答里没有可校验的完整句子")
    elif counts["unsupported"] == counts["claims"]:
        level = UNSUPPORTED
        notes.append(f"全部 {counts['claims']} 句都没有找到原文支撑，已原样保留供人工判断")
    elif counts["needs_check"]:
        level = NEEDS_CHECK
        head = "、".join(m["text"] for m in missing[:2])
        notes.append(f"{counts['needs_check']} 句需要人工核对：{head}" if head else
                     f"{counts['needs_check']} 句需要人工核对")
        if counts["unsupported"]:
            notes.append(f"另有 {counts['unsupported']} 句没有找到证据（已保留，未删除）")
    elif counts["unsupported"]:
        level = NEEDS_CHECK
        notes.append(f"{counts['unsupported']} 句没有找到证据（已保留，未删除）")
    else:
        level = VERIFIED
        notes.append(f"全部 {counts['claims']} 句都能在原文中直接找到对应句子")

    score = (sum(scores) / len(scores)) if scores else 0.0
    return {
        "applicable": True,
        "level": level,
        "label": LABELS[level],
        "description": DESCRIPTIONS[level],
        "score": round(score, 4),
        "counts": counts,
        "missing": missing,
        "notes": notes,
        "signals": signals,
        "config": config_block(),
    }


def summarize(claims: Iterable[dict[str, Any]]) -> dict[str, int]:
    """计数。保留 `supported` 键：老日志、老前端仍在读它。"""
    total = verified = needs = unsupported = 0
    for c in claims:
        total += 1
        lvl = (c.get("confidence") or {}).get("level")
        if lvl == VERIFIED:
            verified += 1
        elif lvl == NEEDS_CHECK:
            needs += 1
        elif lvl == UNSUPPORTED:
            unsupported += 1
        elif c.get("status") == "supported":
            verified += 1
        else:
            unsupported += 1
    return {
        "claims": total,
        "verified": verified,
        "needs_check": needs,
        "unsupported": unsupported,
        "supported": verified + needs,
    }
