"""
不确定性状态：把「不知道」做成返回值，而不是让它表现成一句自信的回答。

三类状态对应链路上三段**不同的**能力边界，不是同一种失败的三种说法：

| 状态 | 断在哪一层 | 用户真正想知道的事 | 下一步动作 |
| --- | --- | --- | --- |
| `empty` | 检索层 | 是库里没有，还是没检索到？检索了什么范围、哪些词没命中？ | 换一种问法（按钮直接可点） |
| `conflict` | 证据层 | 库里有，但两篇论文说的不一样，该信哪个？ | 并排看两处原文，自己判断 |
| `unreadable` | 解析层 | 库里明明有这一页，为什么答不出表里的数？ | 看原页（PDF 直接跳到那一页） |

三条设计约束（来自需求，也是这一版最容易被做错的地方）：

1. **不得用「抱歉，我无法回答」统一兜底。** 那句文案把三种情况压成一种，
   用户既不知道是"没有"还是"没找到"，也不知道下一步该干什么。
   所以三类状态各有独立的判定条件、独立的组件、独立的动作。
2. **判定必须在后端完成。** 前端只拿得到 top-k 片段，看不到"库里一共几篇、关键词命中几次、
   这个符号在几篇论文里取过几个值"——这些正是三类的判据，放在前端等于把判据实现两遍，
   改一处必漂移。更实际的是：**空检索会改变要不要生成**，前端没有这个权力。
3. **不下判断。** 冲突状态只并列展示两边的结论与各自出处，
   连"哪边更可能对"都不给。给的是"你该去看哪两页"。

与 07（`rag/confidence.py`）的分工，别混淆：
`confidence` 判断的是**这一句结论有多少证据支撑**（已验证/需核对/证据不足），
它假设"证据本身是清楚的"；`uncertainty` 判断的是**证据本身是否可用**——
有没有、冲不冲突、解析出来没有。一句话可以是 `verified` 但落在一张没复原的表格上，
这时候两个状态都要显示：前者说"这句话有原文支持"，后者说"那段原文本身是残缺的"。
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

from app.core.config import settings

# 状态版本号。三类判据里任何一条改了都要 bump：
# 复盘时"当时为什么判成空检索"只能靠这个号找回当时的口径，
# 只存判据结果不存版本，等于半年后无法解释自己。
UNC_VERSION = "unc-v1.0.0"

# 三类状态的判定条件与口径。原样发给前端（界面上"为什么这样判"那一块），
# 也用于文档 —— 判据写在代码里、界面上看不见，就等于黑盒。
RULES: list[dict[str, Any]] = [
    {
        "state": "empty",
        "title": "空检索",
        "when": "① 一条片段都没召回；或 ② 检索偏弱（融合原始分低于该策略阈值）且问句的检索关键词在库内命中 0 段",
        "why": "② 是实际中最常出现的形态：dense 永远会返回最近邻，所以『零片段』几乎不会发生。"
               "真正该说『没有』的时候，是关键词在库里一次都没出现过——那些最近邻不是证据。",
        "reads": "命中表里 0 的那一行就是原因；不是排序不好，是库里没有这个词。",
        # {papers} 在 _rules() 里换成这一轮真实的「库内这 5 篇论文」。
        # 写死成「这 N 篇」的话，界面上就直接显示一个字母 N —— 规则是给人核对判据用的，
        # 数字对不上整段就变成模板腔。
        "next": "用改写建议重问，或先看{papers}覆盖了什么",
    },
    {
        "state": "conflict",
        "title": "证据冲突",
        "when": "同一符号/指标在 ≥2 篇论文里取到不同值（取值冲突）；或同一对象上两段结论方向相反（方向冲突）",
        "why": "取值不同不等于有人错了。β2 在 Attention 里是 0.98、在 BERT 里是 0.999——"
               "这是两篇论文各自的实验设置。系统没有能力判断『你要复现哪一篇』，所以只并列，不裁决。",
        "reads": "先看 dimension（争的是哪一维），再看两边的来源论文与页码。",
        "next": "点『并排看原文』，两边各看一眼实验设置",
    },
    {
        "state": "unreadable",
        "title": "表格/图表未识别",
        "when": "召回或引用的片段是 table 但非空单元格 ≤3（结构未复原）、是 figure 但只读到图注、"
                "或该页无文本层（扫描件 / OCR）",
        "why": "这一类片段长得像证据（有页码、有分数、能引用），其实内容是我们没解析出来的东西。"
               "不标出来，用户会把空表格当成「原文没写」。",
        "reads": "看 kind：表格结构未复原 / 只有图注 / 图注是系统生成 / 扫描件。四种的补救方式不同。",
        "next": "看原页——PDF 直接跳到那一页，比任何转述都可靠",
    },
]

# 三类状态的文案表（交付物 3）。前端不再自己拼句子，改文案只改这一处。
COPY: dict[str, dict[str, str]] = {
    "empty.no_corpus": {
        "title": "库内还没有可检索的正文",
        "detail": "这个课题下没有已解析完成的论文片段，检索无从谈起。",
    },
    "empty.no_lexical_hit": {
        "title": "关键词在库内没有出现过",
        "detail": "检索到的片段只是向量空间里的近邻，不是证据——问句里最具体的词，在库里一次都没出现过。",
    },
    "empty.no_hits": {
        "title": "这次检索没有召回任何片段",
        "detail": "关键词在库内出现过，但检索链路没把片段召回，属于系统侧的问题而不是库里没有。",
    },
    # 生成之后才判得出来的一类"空"：候选片段都在，词也命中过，但模型读完给了一句
    # "无法回答"。这不是拒答策略生效（那个走 no_lexical_hit），是证据不够格。
    # 单独列一条，是为了界面上别把"我们主动拒答"和"模型无米下锅"混成一句话。
    "empty.model_declined": {
        "title": "库里没有能支撑这个问题的内容",
        "detail": "召回到的片段里提到了相关词，但没有任何一段真正回答了这个问题；模型据此拒答，"
        "这里把它标成「没有」而不是留一句「无法回答」。",
    },
    "unreadable.table_empty": {
        "title": "表格结构未复原",
        "detail": "这一页的表格只抽到了分隔线，单元格里的数字没有还原出来，表格结构未复原。",
    },
    "unreadable.figure_caption_only": {
        "title": "只读到图注",
        "detail": "图里的曲线、柱子和标注是像素，不是文本；能读到的只有图题那一行。",
    },
    "unreadable.figure_synthetic": {
        "title": "图注是系统生成的",
        "detail": "原文这一页没有可提取的图题，这段说明是入库时由模型生成的模板文案，不是原文。",
    },
    "unreadable.scan_page": {
        "title": "该页为扫描件",
        "detail": "该页为扫描件：没有文本层，文字来自 OCR，个别字符与表格结构可能有错。",
    },
}

# ---------------------------------------------------------------------------
# 一、空检索
# ---------------------------------------------------------------------------

# 只过滤功能词。**不**过滤 model / paper / method 这类词：
# 它们在问句里是内容词（"which model is better" 的 model 就是被问的对象），
# 滤掉会让"关键词全 miss"这个判据在没有内容词时误触发。
_LATIN_STOP = {
    "the", "and", "for", "with", "what", "when", "which", "who", "how", "why",
    "does", "did", "do", "is", "are", "was", "were", "be", "been", "being",
    "from", "that", "this", "these", "those", "there", "here", "has", "have", "had",
    "can", "could", "should", "would", "will", "may", "might", "must",
    "about", "into", "than", "then", "them", "they", "their", "its", "our", "your",
    "not", "no", "yes", "any", "all", "some", "more", "most", "much", "many",
    "use", "used", "using", "between", "during", "after", "before", "under",
    "please", "tell", "show", "give", "explain", "describe",
    # 问句里的空壳动词/副词：留下它们只会让"命中一个词"看起来像检索成功
    "set", "given", "exactly", "respectively", "also", "only", "very", "such",
    "make", "made", "take", "taken", "get", "got", "well", "like", "both", "each",
}

_LATIN_TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-]{2,}")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
# 中文功能词：按"字"切而不是按词切，因为这里没有分词器。
_CJK_STOP_CHARS = "的了是在和与及或也都就而并且把被对关于这那些什么怎么如何多少是否请问"
_QUESTION_TAIL = re.compile(r"[?？。!！,，;；:：]+$")


def _latin_terms(text: str, limit: int) -> list[str]:
    """从检索文本里取内容词。保序、去重、去功能词。"""
    out: list[str] = []
    seen: set[str] = set()
    for m in _LATIN_TERM_RE.finditer(text or ""):
        tok = m.group(0).strip("-").lower()
        if len(tok) < settings.unc_min_term_len or tok in _LATIN_STOP or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
        if len(out) >= limit:
            break
    return out


def _cjk_terms(text: str, limit: int) -> list[str]:
    """
    中文问句里的实词片段。只用于展示（"你原本问的是这几个词"），
    不参与"关键词是否命中"的判定——库内正文是英文，拿中文去库里数命中率，
    结果恒为 0，那会把每一句中文问句都判成空检索。
    """
    out: list[str] = []
    seen: set[str] = set()
    for run in _CJK_RUN_RE.findall(text or ""):
        for frag in re.split(f"[{_CJK_STOP_CHARS}]", run):
            frag = frag.strip()
            if len(frag) < 2 or frag in seen:
                continue
            seen.add(frag)
            out.append(frag)
            if len(out) >= limit:
                break
    return out


def _corpus_chunks(corpus: dict[str, Any] | None) -> list[dict[str, Any]]:
    return list((corpus or {}).get("chunks") or [])


def _payload_text(pl: dict[str, Any]) -> str:
    """和规则召回同一套取文方式：正文 + 图注 + 图注描述都算。"""
    return " ".join(
        x for x in (pl.get("text") or "", pl.get("caption") or "", pl.get("description") or "") if x
    )


_TERM_RE: dict[str, re.Pattern[str]] = {}


def _term_hit(term: str, hay: str) -> bool:
    """
    关键词是否出现在这段正文里——**整词**判定，容忍复数与连字符。

    为什么不能用 `term in hay`：
    `rag` 会命中 `sto-rag-e`、`ave-rag-e`；`bert` 会命中 `Ro-bert-a`；
    `vit` 会命中 `ac-ti-vit-y`。这种子串命中一旦计入"库里有没有这个词"，
    判定就永远说"有"——实测「RAG 如何缓解大模型幻觉」在只有 5 篇
    Transformer/BERT/LoRA/ViT 论文的库里，`rag` 靠着 storage/average 拿到几百段命中，
    于是"最具体的词都出现过"，空检索判不出来，最后是模型硬着头皮答一句"无法回答。"。

    反过来也不能要求严格 \b：论文里 `hallucinations` / `token` / `tokens` 混着写，
    复数形式必须算命中，否则会把"有"判成"没有"，那是误拒，比漏判更伤。
    """
    pat = _TERM_RE.get(term)
    if pat is None:
        pat = re.compile(rf"(?<![a-z0-9]){re.escape(term)}(?:e?s)?(?![a-z0-9])")
        _TERM_RE[term] = pat
    return pat.search(hay) is not None


def _count_hits(chunks: Sequence[dict[str, Any]], terms: Sequence[str]) -> dict[str, int]:
    """每个词命中了多少**段**（不是多少次）。段数比词频更能说明"这个说法在库里存不存在"。"""
    counts = {t: 0 for t in terms}
    if not terms:
        return counts
    for rec in chunks:
        hay = _payload_text(rec.get("payload") or {}).lower()
        if not hay:
            continue
        for t in terms:
            if _term_hit(t, hay):
                counts[t] += 1
    return counts


def _coverage(chunks: Sequence[dict[str, Any]], terms: Sequence[str]) -> tuple[int, int]:
    """
    问句关键词在库内的**同段覆盖度**：返回 (单段最多同时命中几个词, 至少命中一个词的段数)。

    为什么不用"每个词是否命中"来判：问句里总有泛词。
    「蛋白质折叠的自由能垒」译成英文后是 free / energy / barrier / protein / folding，
    其中 energy 在这套语料里到处都是（光纤论文里全是 energy）——
    只要拿"有一个词命中就算检索到了"去判，这句永远判不成空检索，
    而实际上 protein、folding、barrier 三个词在库里一次都没出现过。

    真正的判据是**共现**：一段真正讲这件事的正文，至少会同时出现你问的两个词。
    "只命中一个泛词"的片段不是证据，是词面巧合。
    """
    if not terms:
        return 0, 0
    best, any_hit = 0, 0
    for rec in chunks:
        hay = _payload_text(rec.get("payload") or {}).lower()
        if not hay:
            continue
        n = sum(1 for t in terms if _term_hit(t, hay))
        if n:
            any_hit += 1
            best = max(best, n)
    return best, any_hit


def _scope_of(corpus: dict[str, Any] | None) -> dict[str, Any]:
    """已检索范围：用户问"你们到底搜了什么"时，这一块就是回答。"""
    chunks = _corpus_chunks(corpus)
    papers = (corpus or {}).get("papers") or {}
    by_kind: dict[str, int] = {}
    for rec in chunks:
        kind = (rec.get("payload") or {}).get("kind") or "unknown"
        by_kind[kind] = by_kind.get(kind, 0) + 1
    titles = [
        {"arxiv_id": aid, "title": (p or {}).get("title") or ""}
        for aid, p in sorted(papers.items())
    ]
    return {
        "papers": len(papers),
        "chunks": len(chunks),
        "by_kind": dict(sorted(by_kind.items(), key=lambda kv: -kv[1])),
        "titles": titles[:8],
    }


def _nearest(sources: Sequence[Any], limit: int = 3) -> list[dict[str, Any]]:
    """
    空检索时也要给"系统看到了什么"：这几段是最近邻，不是证据。
    不给的话，用户没法判断"是我词写错了，还是库里真没有"。
    """
    out = []
    for s in sources[:limit]:
        out.append(
            {
                "chunk_id": getattr(s, "chunk_id", ""),
                "arxiv_id": getattr(s, "arxiv_id", ""),
                "title": getattr(s, "title", ""),
                "page": getattr(s, "page", 0),
                "kind": getattr(s, "kind", ""),
                "score": round(float(getattr(s, "score", 0.0) or 0.0), 4),
                "preview": (getattr(s, "text", "") or "")[:140],
            }
        )
    return out


def _suggestions(
    question: str,
    terms: Sequence[str],
    counts: dict[str, int],
    corpus: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """
    改写建议。**每条都必须是能直接点的一问**，不能只是"换个说法试试"这种废话。

    生成顺序就是可信度顺序：
      ① `spelling` 问句里的词和库里某个词只差一两个字（拼错/单复数/缩写）→ 直接替换；
      ② `split`    复合问句拆成第一个子问句（多问题混在一句里时，命中率会被拉平）；
      ③ `topic`    退到主题层：库里有 N 篇论文，问它们能答的问题。
    前两条是"改",第三条是"换"——没有可改的余地时才用第三条，而不是一上来就让用户重来。
    """
    out: list[dict[str, Any]] = []
    vocab = _vocab(corpus)
    missed = [t for t in terms if counts.get(t, 0) == 0]

    # ① 拼写/词形近似
    if missed and vocab:
        for bad in missed:
            best, best_sim = None, 0.0
            for good in vocab:
                if abs(len(good) - len(bad)) > 3:
                    continue
                sim = _ratio(bad, good)
                if sim > best_sim:
                    best, best_sim = good, sim
            if best and best_sim >= 0.72:
                q = re.sub(re.escape(bad), best, question, flags=re.I)
                if q != question:
                    out.append(
                        {
                            "id": "spelling",
                            "kind": "spelling",
                            "label": f"把「{bad}」改成「{best}」",
                            "query": q,
                            "why": f"库内出现过「{best}」，没有出现过「{bad}」——像是拼写或词形不同。",
                        }
                    )
                    break

    # ② 拆句
    parts = [
        p.strip(" ，,、；;")
        for p in re.split(r"[?？；;]|\b(?:and|vs\.?|versus)\b|以及|和|与|、|，", question)
        if p and p.strip()
    ]
    if len(parts) >= 2:
        head = parts[0]
        if head and head != question.strip() and len(head) >= 4:
            out.append(
                {
                    "id": "split",
                    "kind": "split",
                    "label": f"只问前半句：「{head[:24]}」",
                    "query": head,
                    "why": "复合问句里两件事的关键词会互相稀释，拆开分别问命中率更高。",
                }
            )

    # ③ 退到主题层：用库内真实存在的论文名造句，保证按钮点下去一定有东西
    #    （"库里没有"这件事，最有用的一句补充就是"那库里有什么"）
    titles = (corpus or {}).get("papers") or {}
    templates = ("{n} 的主要贡献是什么？", "{n} 用了什么实验设置？")
    for i, (aid, meta) in enumerate(sorted(titles.items())):
        if len(out) >= 3:
            break
        name = (meta or {}).get("title") or aid
        short = re.split(r"[:：]", name)[0].strip() or name
        out.append(
            {
                "id": f"topic{i}",
                "kind": "topic",
                "label": f"改问「{_clip(short)}」",
                "query": templates[i % len(templates)].format(n=short),
                "why": f"这篇在库内，问题一定有答案（库内共 {len(titles)} 篇）。",
            }
        )

    return out[:3]


def _vocab(corpus: dict[str, Any] | None) -> list[str]:
    """库内词表：取标题里的实词。几百个词的规模，够做拼写近似，也不贵。"""
    out: set[str] = set()
    for meta in ((corpus or {}).get("papers") or {}).values():
        for tok in _LATIN_TERM_RE.findall((meta or {}).get("title") or ""):
            tok = tok.lower()
            if len(tok) >= settings.unc_min_term_len and tok not in _LATIN_STOP:
                out.add(tok)
    return sorted(out)


def _clip(text: str, limit: int = 18) -> str:
    """按词边界截断：切到半个单词（"Attention Is All Y"）看起来像乱码。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip("，,·-") + "…"


def _ratio(a: str, b: str) -> float:
    """很轻的相似度：公共前缀 + 共同字符占比。不做编辑距离，几百次对比不值得。"""
    if not a or not b:
        return 0.0
    common = len(set(a) & set(b)) / max(len(set(a)), len(set(b)))
    prefix = 0
    for x, y in zip(a, b):
        if x != y:
            break
        prefix += 1
    return max(common, prefix / max(len(a), len(b)))


def empty_state(
    *,
    question: str,
    search_text: str,
    plan_type: str,
    sources: Sequence[Any],
    retrieval_weak: bool,
    top_raw: float | None,
    threshold: float | None,
    corpus: dict[str, Any] | None,
    strategy: str | None = None,
    translated: bool = False,
) -> dict[str, Any] | None:
    """
    判定"这次算不算空检索"。返回 None 表示正常作答。

    三种 reason：
      `no_corpus`      库是空的（没入库完成 / 没解析出片段）
      `no_lexical_hit` 关键词在库内 0 命中，且检索偏弱 —— 实际中最常见的那种"没有"
      `no_hits`        关键词在库内，但一条都没召回（系统侧问题）

    `blocking` 决定要不要跳过生成：库里确实没有的东西，让模型拿着 6 段近邻去写，
    它只会写出"根据现有资料无法确定"这种既不像拒答也不像答案的东西——
    还不如把"没有"和"为什么没有"讲清楚。
    """
    chunks = _corpus_chunks(corpus)
    if corpus is None:
        # 语料快照取不到（Qdrant 抖动 / 索引重建中）：**判不了**"库里到底有没有"。
        # 这时候不能报 no_corpus —— 那是在替一个自己不知道的事实下结论。
        # 有片段就照常作答（少一条提示比多一条假提示好），没片段才说"这次没召回"。
        if sources:
            return None
        return _empty_payload(
            "no_hits", question, search_text, plan_type, [], {}, _scope_of(None),
            sources, None, strategy, translated, blocking=True, top_raw=top_raw, threshold=threshold,
        )
    if not chunks:
        return _empty_payload(
            "no_corpus", question, search_text, plan_type, [], {}, _scope_of(corpus),
            sources, corpus, strategy, translated, blocking=True, top_raw=top_raw, threshold=threshold,
        )

    terms = _latin_terms(search_text or question, settings.unc_max_terms)
    counts = _count_hits(chunks, terms)
    coverage, chunks_any = _coverage(chunks, terms)
    # 只有一个关键词时"共现"不成立，退化成"它自己有没有出现"
    need = min(2, len(terms))
    # 区分度最高的两个词：df 最低、同分取更长的（长词更具体）。
    # 为什么盯这两个而不是"有没有任一词命中"：问句里总有泛词。
    # 「蛋白质折叠的自由能垒」译成英文是 free / energy / barrier / protein / folding，
    # 其中 free energy 在光纤论文里是正经术语（自由能），能命中；
    # 而 protein、folding 一次都没有。只要拿"命中一个就算找到"去判，这句永远判不成空检索。
    rare = sorted(terms, key=lambda t: (counts.get(t, 0), -len(t)))[:2]
    rare_missing = bool(rare) and all(counts.get(t, 0) == 0 for t in rare)
    no_coverage = coverage < need

    if sources:
        if not terms:
            return None
        # 判"这批片段算不算证据"看两层，任一层成立即判空：
        #   ① 没有任何一段同时出现两个关键词（no_coverage）——最强，与检索分无关；
        #   ② 最具体的两个词全库 0 命中（rare_missing），且检索本身偏弱。
        # ② 为什么还要挂在 retrieval_weak 上：光看词频会误杀可答题——实测评测集里
        # p13（classification/claim）与 s02（parameter-efficient/full-parameter）两个
        # 最稀有的词都是 0 命中，但答案确实在库里（表头被切成了 cell kind）。词频只证明
        # "这个词没出现"，不证明"这件事没有"。
        # 但也别指望 retrieval_weak 兜住一切：它用的是 RRF 融合分，只反映名次不反映语义，
        # 库里完全没答案的问题照样能凑到 0.032 > 阈值 0.03（见「RAG 如何缓解大模型幻觉」）。
        # 剩下那类"模型自己说答不了"的情况，交给生成后的 model_declined 兜底。
        if not no_coverage and not (rare_missing and retrieval_weak):
            return None
        if not settings.unc_empty_strict:
            # 严格模式关掉：只保留"一条都没召回"这一种空检索（旧口径）。
            # 保留它是为了 A/B：把"关键词覆盖不足"也算空检索，到底救回多少幻觉。
            return None

    reason = "no_lexical_hit" if (no_coverage or rare_missing) else "no_hits"
    return _empty_payload(
        reason, question, search_text, plan_type, terms, counts, _scope_of(corpus),
        sources, corpus, strategy, translated, blocking=True, top_raw=top_raw, threshold=threshold,
        coverage={
            "best_chunk_terms": coverage,
            "chunks_any": chunks_any,
            "need": need,
            "rare": rare,
            "rare_missing": rare_missing,
        },
    )


def declined_state(
    *,
    question: str,
    search_text: str,
    plan_type: str,
    sources: Sequence[Any],
    corpus: dict[str, Any] | None,
    strategy: str | None = None,
    translated: bool = False,
    top_raw: float | None = None,
    threshold: float | None = None,
    evidence: str | None = None,
) -> dict[str, Any]:
    """
    生成之后才知道的那一类"空"：模型读完候选片段，回了一句"无法回答"。

    为什么不在生成前判：判不出来。评测集上标定过两轮——
      · 词频信号：可答题里 p13（classification/claim）、s02（parameter-efficient）两个
        最稀有的词同样 0 命中，阈值一放宽就误拒 2/36；
      · 精排 logit：可答题 top 分数从 -9.56 到 7.85 铺满，不可答题 -11.18 到 +0.9，
        重叠严重，任何单点阈值都会同时吃掉两边。
    所以在生成后兜底：全部 claim 都没被原文支撑、且答案正文本身就是一句拒答时，
    把它从"一句没有信息的无法回答"转成"库里没有 + 检索范围 + 改写建议"。
    可答题不受影响——它们至少有 1 条 supported claim。
    """
    chunks = _corpus_chunks(corpus)
    terms = _latin_terms(search_text or question, settings.unc_max_terms)
    counts = _count_hits(chunks, terms) if chunks else {t: 0 for t in terms}
    coverage, chunks_any = _coverage(chunks, terms) if chunks else (0, 0)
    need = min(2, len(terms))
    rare = sorted(terms, key=lambda t: (counts.get(t, 0), -len(t)))[:2]
    payload = _empty_payload(
        "model_declined",
        question,
        search_text,
        plan_type,
        terms,
        counts,
        _scope_of(corpus),
        sources,
        corpus,
        strategy,
        translated,
        blocking=True,
        top_raw=top_raw,
        threshold=threshold,
        coverage={
            "best_chunk_terms": coverage,
            "chunks_any": chunks_any,
            "need": need,
            "rare": rare,
            "rare_missing": bool(rare) and all(counts.get(t, 0) == 0 for t in rare),
        },
    )
    if evidence:
        payload["detail"] = f"{payload['detail']}（模型原话：{evidence[:60]}）"
    return payload


def _empty_payload(
    reason: str,
    question: str,
    search_text: str,
    plan_type: str,
    terms: Sequence[str],
    counts: dict[str, int],
    scope: dict[str, Any],
    sources: Sequence[Any],
    corpus: dict[str, Any] | None,
    strategy: str | None,
    translated: bool,
    *,
    blocking: bool,
    top_raw: float | None,
    threshold: float | None,
    coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    copy = COPY[f"empty.{reason}"]
    terms_view = [
        {
            "term": t,
            "chunks": counts.get(t, 0),
            "hit": counts.get(t, 0) > 0,
        }
        for t in terms
    ]
    return {
        "reason": reason,
        "title": copy["title"],
        "detail": copy["detail"],
        "blocking": blocking,
        "scope": scope,
        "query": {
            "question": question,
            "search_text": search_text,
            "translated": translated,
            "type": plan_type,
            "strategy": strategy,
            "terms": terms_view,
            "zh_terms": _cjk_terms(question, 6),
            "top_raw_score": None if top_raw is None else round(top_raw, 6),
            "threshold": threshold,
            # 同段覆盖：0 表示没有任何一段同时出现两个关键词
            "coverage": coverage or {"best_chunk_terms": 0, "chunks_any": 0, "need": min(2, len(terms))},
        },
        # 最近邻单独放：它们的身份是"系统看到了什么"，不是"证据"
        "nearest": _nearest(sources),
        "suggestions": _suggestions(question, terms, counts, corpus),
        "action": {
            "type": "rewrite",
            "label": "用建议改问" if reason != "no_corpus" else "去上传论文",
        },
        "version": UNC_VERSION,
    }


# ---------------------------------------------------------------------------
# 二、证据冲突
# ---------------------------------------------------------------------------

# 数值后面紧跟的单位。用来区分"同一个量取不同值"和"同名不同量"——
# β2 = 0.98（无量纲，Adam 的二阶矩衰减）与 β2 = 0.1 ps²/nm/km（色散系数）
# 是**两个不同的物理量**，把它们并列成"冲突"是错的。
_UNIT_RE = re.compile(
    r"^\s*(ps\s*[²2]\s*/\s*nm\s*/\s*km|ps\s*/\s*nm\s*/\s*km|%|ms\b|s\b|nm\b|km\b|"
    r"GB\b|MB\b|TB\b|B\b|M\b|K\b|倍|times)",
    re.I,
)
_NUM_RE = re.compile(r"[-−–]?\s*\d+(?:[.,]\d+)?(?:\s*[×xX]\s*10\s*[-−^]?\s*\d+)?")
# 值后面这一段用来给用户看上下文："0.98 and ϵ = 10−9" / "0.1 ps2/nm/km are Kerr nonlinear"
_CTX_CHARS = 46
_VALUE_WINDOW = 26          # 符号到数值的最大距离；再远就不是"这个符号的值"了

# 方向词表。中文答案为主，论文里的英文原句通过证据句单独兜（见 _polars）。
_POS = (
    "优于", "超过", "好于", "强于", "提升", "提高", "改善", "增强", "有效", "增益",
    "更快", "更好", "更高", "更少", "降低", "减少", "缓解", "解决",
)
_NEG = (
    "不如", "低于", "劣于", "差于", "弱于", "下降", "变差", "退化", "无效", "失效",
    "更慢", "更差", "更低", "更多", "增加", "加剧", "没能", "未能", "无法",
)
# 条件标记：出现它就说明这句话只在某个范围内成立，
# 那样的两句"方向相反"不是矛盾，是条件不同（`conditional_split`）。
_COND = (
    "在", "当", "如果", "若", "一旦", "情况下", "条件下", "时", "小数据", "大数据",
    "if ", "when ", "under ", "with ", "small ", "large ",
)

TYPE_VALUE = "value_diff"
TYPE_POLARITY = "polarity"
TYPE_CONDITIONAL = "conditional_split"
TYPE_SCOPE = "scope_mismatch"

CONFLICT_LABELS = {
    TYPE_VALUE: "取值不同",
    TYPE_POLARITY: "结论相反",
    TYPE_CONDITIONAL: "条件性分歧",
    TYPE_SCOPE: "同名不同义",
}


def _norm_value(raw: str) -> str:
    v = raw.replace("−", "-").replace("–", "-").replace(" ", "")
    v = v.replace(",", "")
    # 0.980 → 0.98：同一个值写成不同位数不该被当成两个值
    if "." in v:
        v = v.rstrip("0").rstrip(".")
    return v or raw


# 符号与数值之间必须有一个"赋值"关系，才认它是这个符号的取值。
#
# 两个真实的反例决定了这张表能收哪些词：
#   `β2 and β3 = 0.1 ps2/nm/km` → 只要求"附近有数字"会给 β2 抽到 3（β3 的下标）；
#   `β2 of 10 m SMF`            → 用 `of` 当分隔符会把光纤**长度** 10 m 当成 β2 的值，
#                                 而它的值在句末（−20 ps/nm/km）。
# 所以只认 `= : | is set to` 这一档明确的赋值。代价是"β2 取 0.98"这类纯散文写法读不到——
# 可以接受：读不到只是少一处冲突提示，读错会报出一处假冲突，两者代价不对称。
_ASSIGN_RE = re.compile(r"\s*(?:=|:|＝|\||is\b|was\b|were\b|set\s+to|taken\s+as)\s*", re.I)


def _values_after(text: str, pattern: str) -> list[dict[str, Any]]:
    """找出 text 里所有 `符号 = 数值 单位` 的形态，带上下文。"""
    out: list[dict[str, Any]] = []
    for m in re.finditer(pattern, text, re.I):
        tail = text[m.end(): m.end() + _VALUE_WINDOW]
        sep = _ASSIGN_RE.match(tail)
        if not sep:
            continue
        rest = tail[sep.end():]
        num = _NUM_RE.search(rest[:16])
        if not num:
            continue
        unit_m = _UNIT_RE.match(rest[num.end():])
        unit = (unit_m.group(1) if unit_m else "").strip()
        after = rest[num.end(): num.end() + _CTX_CHARS]
        out.append(
            {
                "value": num.group(0).strip(),
                "norm": _norm_value(num.group(0)),
                "unit": unit,
                "context": re.sub(r"\s+", " ", after).strip(),
                "at": m.start(),
            }
        )
    return out


def _symbol_probes(question: str) -> list[Any]:
    """
    只取符号类探针做冲突检测。

    指标数值（"BLEU 28.4"）也很多义，但它在问句里往往已经带了限定
    （"WMT14 英德上的 BLEU"），按符号判会误报；符号类的最干净：
    β2 就是 β2，跨论文比较它才有意义。
    """
    try:
        from app.services.rag.numeric_rules import scan_query

        return [p for p in scan_query(question) if p.kind == "symbol"]
    except Exception:
        return []


def conflicts(
    *,
    question: str,
    sources: Sequence[Any],
    claims: Sequence[dict[str, Any]],
    corpus: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """
    两类冲突一起收：
      `value_diff`  同一符号在 ≥2 篇论文里取到不同值（数值层）
      `polarity`    同一对象上两段结论方向相反（语句层）

    前者是确定性的（正则 + 分单位归组），后者保守（宁可漏，不可误）。
    误报的代价不对称：一次假冲突会让用户怀疑一条本来正确的答案，
    所以方向冲突要求"对象有交集 + 方向相反 + 都没有条件限定"才报。
    """
    out: list[dict[str, Any]] = []
    out.extend(_value_conflicts(question, corpus))
    out.extend(_polarity_conflicts(claims))
    for i, c in enumerate(out, 1):
        c["id"] = f"U{i}"
    return out[: settings.unc_conflict_max]


def _paper_of(pl: dict[str, Any]) -> str:
    return pl.get("arxiv_id") or ""


def _value_conflicts(question: str, corpus: dict[str, Any] | None) -> list[dict[str, Any]]:
    probes = _symbol_probes(question)
    if not probes or not corpus:
        return []
    chunks = _corpus_chunks(corpus)
    if not chunks:
        return []

    out: list[dict[str, Any]] = []
    for probe in probes:
        # 展示用问句里的原样（β2），内部标识用归一化后的 label（beta2）：
        # 界面上写 beta2 是在念我们的变量名，不是在说用户的符号。
        disp = getattr(probe, "raw", "") or probe.label
        occurrences: list[dict[str, Any]] = []
        for rec in chunks:
            pl = rec.get("payload") or {}
            text = _payload_text(pl)
            if not text:
                continue
            for pat in probe.patterns:
                for hit in _values_after(text, pat):
                    occurrences.append(
                        {
                            **hit,
                            "arxiv_id": _paper_of(pl),
                            "title": pl.get("title") or "",
                            "page": pl.get("page") or 0,
                            "chunk_id": rec.get("id") or "",
                            "kind": pl.get("kind") or "",
                            "snippet": text[max(0, hit["at"] - 30): hit["at"] + 90],
                        }
                    )
                if occurrences:
                    break  # 同一个 chunk 只用最匹配的那条 pattern，避免重复计数

        if len(occurrences) < 2:
            continue

        # 按"量纲"分组：无量纲 / ps²/nm/km / %…
        # 只拿**最大的那一组**去比取值，其余进 `ambiguous`（同名不同义）。
        by_unit: dict[str, list[dict[str, Any]]] = {}
        for o in occurrences:
            by_unit.setdefault(o["unit"].lower(), []).append(o)
        main_unit = max(by_unit, key=lambda u: len({o["arxiv_id"] for o in by_unit[u]}))
        main = by_unit[main_unit]
        others = [o for u, group in by_unit.items() if u != main_unit for o in group]

        # 每篇论文取一个代表值（同一篇里出现多次取第一次：那是实验设置段）
        per_paper: dict[str, dict[str, Any]] = {}
        for o in main:
            per_paper.setdefault(o["arxiv_id"], o)
        by_value: dict[str, list[dict[str, Any]]] = {}
        for aid, o in per_paper.items():
            by_value.setdefault(o["norm"], []).append(o)
        if len(by_value) < 2:
            # 值相同或只有一篇：不算冲突。但"同名不同义"仍要说，那是另一种不确定性。
            if others:
                out.append(_ambiguous(probe, per_paper, others))
            continue

        sides = []
        for i, (norm, group) in enumerate(sorted(by_value.items(), key=lambda kv: kv[1][0]["page"])):
            first = group[0]
            sides.append(
                {
                    "value": first["value"],
                    "unit": first["unit"],
                    "conclusion": f"{disp} = {first['value']}" + (f" {first['unit']}" if first["unit"] else ""),
                    "papers": [
                        {"arxiv_id": g["arxiv_id"], "title": g["title"], "page": g["page"]}
                        for g in sorted(group, key=lambda x: x["arxiv_id"])
                    ],
                    "quotes": [
                        {
                            "chunk_id": g["chunk_id"],
                            "arxiv_id": g["arxiv_id"],
                            "title": g["title"],
                            "page": g["page"],
                            "text": re.sub(r"\s+", " ", g["snippet"]).strip(),
                        }
                        for g in group
                    ],
                }
            )
        out.append(
            {
                "kind": TYPE_VALUE,
                "type": TYPE_VALUE,
                "type_label": CONFLICT_LABELS[TYPE_VALUE],
                "dimension": f"{disp} 的取值",
                "probe": probe.label,
                "sides": sides,
                "not_judged": (
                    "两边都是各自论文里的实验设置。系统不判断哪个值适用于你的问题——"
                    "那取决于你要复现哪一篇。"
                ),
                "why": (
                    f"「{disp}」在库内 {len(per_paper)} 篇论文里出现了 {len(by_value)} 个不同取值；"
                    "同一个符号被不同论文赋了不同的数。"
                ),
                "ambiguous": _ambiguous_note(probe, others),
                "action": {"type": "compare_pages", "label": "并排看两处原文"},
            }
        )

    return out


def _disp(probe: Any) -> str:
    """展示名：优先用问句里的原样写法（β₂），退回归一化标识（beta2）。"""
    return getattr(probe, "raw", "") or probe.label


def _ambiguous(probe: Any, per_paper: dict[str, Any], others: list[dict[str, Any]]) -> dict[str, Any]:
    """只有"同名不同义"、没有取值冲突时，也不该装作没看见。"""
    o = others[0]
    disp = _disp(probe)
    return {
        "kind": TYPE_SCOPE,
        "type": TYPE_SCOPE,
        "type_label": CONFLICT_LABELS[TYPE_SCOPE],
        "dimension": f"{disp} 的含义",
        "probe": probe.label,
        "sides": [
            {
                "value": o["value"],
                "unit": o["unit"],
                "conclusion": f"{disp} = {o['value']} {o['unit']}".strip(),
                "papers": [{"arxiv_id": o["arxiv_id"], "title": o["title"], "page": o["page"]}],
                "quotes": [
                    {
                        "chunk_id": o["chunk_id"],
                        "arxiv_id": o["arxiv_id"],
                        "title": o["title"],
                        "page": o["page"],
                        "text": re.sub(r"\s+", " ", o["snippet"]).strip(),
                    }
                ],
            }
        ],
        "not_judged": "这不是冲突：同名符号在两篇论文里指的不是同一个量。系统不合并它们。",
        "why": f"库内另有一篇论文用「{disp}」表示带单位的另一个物理量。",
        "ambiguous": [],
        "action": {"type": "compare_pages", "label": "看那一处原文"},
    }


def _ambiguous_note(probe: Any, others: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not others:
        return []
    seen: dict[str, dict[str, Any]] = {}
    for o in others:
        seen.setdefault(o["arxiv_id"], o)
    return [
        {
            "arxiv_id": o["arxiv_id"],
            "title": o["title"],
            "page": o["page"],
            "unit": o["unit"],
            "value": o["value"],
            "note": f"这里的「{_disp(probe)}」带单位 {o['unit']}，是另一个物理量",
        }
        for o in seen.values()
    ]


def _polars(text: str) -> str | None:
    """返回 'pos' / 'neg' / None。一句话同时出现正反词就判 None —— 那是两个论断挤在一起。"""
    low = (text or "").lower()
    pos = [w for w in _POS if w in low]
    neg = [w for w in _NEG if w in low]
    if pos and not neg:
        return "pos"
    if neg and not pos:
        return "neg"
    return None


def _conditional(text: str) -> bool:
    low = f"{(text or '').lower()} "
    return any(w in low for w in _COND)


def _objects(text: str) -> set[str]:
    """对象 = 拉丁专名（Transformer / BERT / LoRA / ResNet）。中文名词不做对象，抽不准。"""
    out: set[str] = set()
    for tok in _LATIN_TERM_RE.findall(text or ""):
        t = tok.lower()
        if len(t) >= 3 and t not in _LATIN_STOP:
            out.add(t)
    return out


def _polarity_conflicts(claims: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """在**同一批 claim** 里找方向相反的跨论文结论。"""
    items = []
    for c in claims or []:
        text = c.get("text") or ""
        pol = _polars(text)
        if not pol:
            continue
        papers = {ev.get("arxiv_id") for ev in (c.get("citations") or []) if ev.get("arxiv_id")}
        if not papers:
            continue
        objs = _objects(text)
        if not objs:
            continue
        items.append({"claim": c, "text": text, "pol": pol, "papers": papers, "objs": objs})

    out: list[dict[str, Any]] = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            if a["pol"] == b["pol"]:
                continue
            if not (a["papers"] - b["papers"]):
                # 同一篇论文自己前后不一是另一回事（写作错误），不在这里判
                continue
            shared = a["objs"] & b["objs"]
            if not shared:
                continue
            cond = _conditional(a["text"]) or _conditional(b["text"])
            kind = TYPE_CONDITIONAL if cond else TYPE_POLARITY
            out.append(
                {
                    "kind": kind,
                    "type": kind,
                    "type_label": CONFLICT_LABELS[kind],
                    "dimension": "、".join(sorted(shared)[:3]) + " 上的结论方向",
                    "probe": "",
                    "sides": [
                        {
                            "value": "",
                            "unit": "",
                            "conclusion": a["text"][:120],
                            "papers": [
                                {"arxiv_id": p, "title": "", "page": 0}
                                for p in sorted(a["papers"])
                            ],
                            "quotes": [
                                {
                                    "chunk_id": ev.get("chunk_id") or "",
                                    "arxiv_id": ev.get("arxiv_id") or "",
                                    "title": ev.get("title") or "",
                                    "page": ev.get("page") or 0,
                                    "text": ev.get("text") or "",
                                }
                                for ev in (a["claim"].get("citations") or [])[:2]
                            ],
                        },
                        {
                            "value": "",
                            "unit": "",
                            "conclusion": b["text"][:120],
                            "papers": [
                                {"arxiv_id": p, "title": "", "page": 0}
                                for p in sorted(b["papers"])
                            ],
                            "quotes": [
                                {
                                    "chunk_id": ev.get("chunk_id") or "",
                                    "arxiv_id": ev.get("arxiv_id") or "",
                                    "title": ev.get("title") or "",
                                    "page": ev.get("page") or 0,
                                    "text": ev.get("text") or "",
                                }
                                for ev in (b["claim"].get("citations") or [])[:2]
                            ],
                        },
                    ],
                    "not_judged": (
                        "两边各自在自己的条件下成立，差异来自条件而不是谁错了。"
                        if cond
                        else "两段结论方向相反。系统不判断哪边成立——需要你回原文核对实验设置。"
                    ),
                    "why": "同一对象上，两篇论文给出的方向相反。",
                    "ambiguous": [],
                    "action": {"type": "compare_pages", "label": "并排看两处原文"},
                }
            )
    return out


# ---------------------------------------------------------------------------
# 三、表格 / 图表未识别
# ---------------------------------------------------------------------------

_CELL_SPLIT = re.compile(r"[|\n\t]+")


def classify_chunk(text: str, kind: str, anchors: dict[str, Any] | None) -> str | None:
    """
    单段片段的"可读性"。返回 None 表示这段能当证据用。

    四个 kind 对应四种不同的补救方式，所以必须分开判：
      `scan_page`           这一页整个没有文本层 —— 文字本身可能是错的
      `table_empty`         表格结构没复原 —— 数字在页面上，但没进文本
      `figure_caption_only` 只读到图注 —— 图里的数据从来没被解析过
      `figure_synthetic`    图注是模型生成的 —— 最危险：它看起来像原文
    """
    doc = anchors or {}
    if doc.get("text_layer") is False or doc.get("precision") == "ocr":
        return "scan_page"

    body = (text or "").strip()
    if kind == "table":
        cells = [c for c in _CELL_SPLIT.split(body) if c.strip()]
        # 判"空壳"看两件事：非空单元格太少，或整段一个数字都没有。
        # 本语料 80 段 table 里 52 段命中，长这样：`|`、`| | | | | | |`、`BER | T B | ERT`。
        if len(cells) <= 3 or not re.search(r"\d", body):
            return "table_empty"
    if kind == "figure":
        if body.startswith("该图表"):
            return "figure_synthetic"
        if re.match(r"^(?:FIGURE|Figure|Fig\.?)\s*\d", body):
            return "figure_caption_only"
    return None


def unreadable(
    *,
    sources: Sequence[Any],
    anchors_map: dict[str, Any] | None,
    refs: Sequence[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """
    只报**本次真的碰到了**的片段。

    为什么不把所有坏片段都列出来：一次回答只用了 6 段，列出 52 段空表格
    是"系统自检报告"，不是"这次回答要注意什么"。用户只关心：
    我看到的这句话，踩没踩到坑。
    """
    cited = {r.get("chunk_id") for r in (refs or []) if r.get("chunk_id")}
    # 只报"这次回答真的碰过的论文"：回答引用了 ViT 的第 6 页，
    # 那么同一篇的第 7 页空表格值得提醒（它就在旁边，用户很可能顺手翻到）；
    # 但检索顺带捞到的一篇无关论文的空表格不是这条回答的不确定性，是噪声。
    cited_papers = {r.get("arxiv_id") for r in (refs or []) if r.get("arxiv_id")}
    anchors_map = anchors_map or {}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    for s in sources or []:
        cid = getattr(s, "chunk_id", "")
        if cid in seen:
            continue
        if cited and cid not in cited and getattr(s, "arxiv_id", "") not in cited_papers:
            continue
        kind = getattr(s, "kind", "") or ""
        doc = anchors_map.get(cid) or {}
        flag = classify_chunk(getattr(s, "text", "") or "", kind, doc)
        if not flag:
            continue
        seen.add(cid)
        copy = COPY[f"unreadable.{flag}"]
        cells = [c for c in _CELL_SPLIT.split(getattr(s, "text", "") or "") if c.strip()]
        out.append(
            {
                "id": "",
                "kind": flag,
                "title": copy["title"],
                "detail": copy["detail"],
                "severity": "high" if cid in cited else "info",
                "cited": cid in cited,
                "chunk_id": cid,
                "arxiv_id": getattr(s, "arxiv_id", ""),
                "paper_title": getattr(s, "title", ""),
                "page": getattr(s, "page", 0),
                "chunk_kind": kind,
                "evidence": {
                    "chars": len(getattr(s, "text", "") or ""),
                    # cells 只对表格有意义；对 figure 段数"单元格"是拿分隔符硬切出来的假数
                    "cells": len(cells) if kind == "table" else None,
                    "text_layer": doc.get("text_layer", True),
                    "precision": doc.get("precision"),
                    "has_anchors": bool(doc.get("items")),
                },
                "action": {
                    "type": "open_page",
                    "label": "查看原页",
                    "arxiv_id": getattr(s, "arxiv_id", ""),
                    "page": getattr(s, "page", 0),
                },
            }
        )
        if len(out) >= settings.unc_unreadable_max:
            break

    for i, item in enumerate(out, 1):
        item["id"] = f"P{i}"
    # 影响过回答的排前面：那是"你刚看到的结论可能不准"，其余只是"顺带一提"
    out.sort(key=lambda x: (0 if x["cited"] else 1, x["page"]))
    return out


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------

def _rules(corpus: dict[str, Any] | None) -> list[dict[str, Any]]:
    """
    把 RULES 里的占位符换成这一轮的真实数字。

    取不到语料时用不含数字的说法，不编一个篇数出来——规则区是给用户核对判据用的，
    里面出现一个猜出来的数，整块的可信度就没了。
    """
    n = len((corpus or {}).get("papers") or {})
    fill = f"库内这 {n} 篇论文" if n else "库内这些论文"
    return [{**r, "next": r["next"].replace("{papers}", fill)} for r in RULES]


def build(
    *,
    question: str,
    search_text: str,
    plan_type: str,
    sources: Sequence[Any],
    anchors_map: dict[str, Any] | None = None,
    claims: Sequence[dict[str, Any]] | None = None,
    refs: Sequence[dict[str, Any]] | None = None,
    retrieval_weak: bool = False,
    top_raw: float | None = None,
    threshold: float | None = None,
    corpus: dict[str, Any] | None = None,
    strategy: str | None = None,
    translated: bool = False,
) -> dict[str, Any]:
    """
    一次回答的三类状态。返回结构固定，前端不需要"有没有这个字段"地猜。

    `checked` 记录每类**检测了没有**：冲突检测只在问句里带符号时才跑，
    没跑和跑过没发现是两件事，界面上一句"已检测"可能是假的，所以要把这个讲清楚。
    """
    empty = empty_state(
        question=question, search_text=search_text, plan_type=plan_type,
        sources=sources, retrieval_weak=retrieval_weak, top_raw=top_raw,
        threshold=threshold, corpus=corpus, strategy=strategy, translated=translated,
    )
    conf = [] if empty else conflicts(
        question=question, sources=sources, claims=claims or [], corpus=corpus,
    )
    unread = [] if empty else unreadable(sources=sources, anchors_map=anchors_map, refs=refs)

    states = [k for k, v in (("empty", empty), ("conflict", conf), ("unreadable", unread)) if v]
    return {
        "version": UNC_VERSION,
        "states": states,
        "empty": empty,
        "conflicts": conf,
        "unreadable": unread,
        "checked": {
            "empty": True,
            "conflict": {
                "ran": bool(_symbol_probes(question)) or bool(claims),
                "symbols": [p.label for p in _symbol_probes(question)],
                "claims": len(claims or []),
            },
            "unreadable": {"ran": True, "sources": len(sources or [])},
        },
        "rules": _rules(corpus),
    }


def corpus_of(vector_store: Any) -> dict[str, Any] | None:
    """
    取语料快照。拿不到就返回 None —— 状态检测是"附加信息"，
    它失败绝不能把一次正常的问答变成 500。宁可这一轮不报空检索，
    也不能让用户看不到答案。
    """
    try:
        from app.services.rag.strategies import load_corpus

        return load_corpus(vector_store)
    except Exception:
        return None


def reason_text(empty: dict[str, Any]) -> str:
    """
    空检索的一句话摘要，给 `reject_reason` 这个老字段用。

    刻意不写"无法回答"：那是把三种情况压成一句的兜底，用户看完仍不知道
    "是库里没有"还是"系统没搜到"。这里写的是可核对的事实（几篇、几段、哪些词 0 命中），
    老前端即便不认识新组件，读到的也是一句有信息量的话。
    """
    q = empty.get("query") or {}
    scope = empty.get("scope") or {}
    reason = empty.get("reason")
    if reason == "no_corpus":
        return "这个课题下还没有已解析完成的论文片段，检索无从谈起。"
    if reason == "model_declined":
        missed = [f"「{t['term']}」" for t in (q.get("terms") or []) if not t.get("hit")]
        tail = f"（{'、'.join(missed[:3])} 在库内没有出现过）" if missed else ""
        return (
            f"库内 {scope.get('papers', 0)} 篇论文共 {scope.get('chunks', 0)} 个片段里，"
            f"没有能回答这个问题的内容{tail}，模型已按证据不足拒答。"
        )
    missed = [t["term"] for t in (q.get("terms") or []) if not t.get("hit")]
    head = f"在库内 {scope.get('papers', 0)} 篇论文的 {scope.get('chunks', 0)} 个片段里，"
    if reason == "no_lexical_hit":
        cov = q.get("coverage") or {}
        parts = []
        rare = [t for t in (cov.get("rare") or []) if t in missed]
        if rare:
            parts.append("、".join(f"「{t}」" for t in rare) + " 一次都没出现")
        elif missed:
            parts.append("、".join(f"「{t}」" for t in missed[:4]) + " 一次都没出现")
        parts.append(
            f"没有任何一段同时出现你问的两个关键词（最高 {cov.get('best_chunk_terms', 0)} 个）"
        )
        return head + "，".join(parts) + "，因此没有作答。"
    return head + "这次检索没有召回任何可用的内容。"


def none_payload() -> dict[str, Any]:
    """
    "不适用"的完整形态（闲聊/直答）。
    仍然把 rules 带出去：同一份响应结构，前端不必为"没有状态"写第二套分支。
    """
    return {
        "version": UNC_VERSION,
        "states": [],
        "empty": None,
        "conflicts": [],
        "unreadable": [],
        "checked": {"empty": False, "conflict": {"ran": False, "symbols": [], "claims": 0}, "unreadable": {"ran": False, "sources": 0}},
        # 闲聊路径拿不到语料，篇数留空说法，不编数字。
        "rules": _rules(None),
    }


__all__ = [
    "CONFLICT_LABELS",
    "COPY",
    "RULES",
    "UNC_VERSION",
    "build",
    "classify_chunk",
    "conflicts",
    "corpus_of",
    "empty_state",
    "none_payload",
    "reason_text",
    "unreadable",
]
