"""
句子层：切句、解析行内引用、判定 claim / bridge、数值落地校验。

这一层是"可追溯"真正落地的地方。四个判断都在这里做，而且全部是**确定性代码**，
不依赖模型自觉：

1. **切句要连引用一起切**。模型写的是「……更稳定。[引#3]」——引用在句号之后。
   只在句号处切会把 `[引#3]` 甩到下一句，于是本句被判定"没有引用"而删除、
   下一句凭空多一个引用。综述模块踩过这个坑（一次丢掉 15 个簇的首句），这里照抄结论。

2. **bridge 不是免死金牌**。任务要求过渡句/连接句标"无需引用"，模型立刻会发现
   这是个好东西——把所有难写的判断句都标成 bridge 就都不用给引用了。
   所以服务端复核：含数字、比较、效果断言的句子一律**改判 claim**；
   改判后没有引用 → 整句删除。桥可以没有引用，但桥不能承载结论。

3. **数字必须落地**。正文里的百分比/小数/倍数必须能在**它自己引的那几段**原文里找到，
   找不到就整句删除。这是"编造 3.2% 提升"这类幻觉唯一能自动拦住的地方。

4. **`[12]` 这类编号要清掉**。模型有时会按论文习惯写方括号数字引用，
   那是它自己脑补的文献序号——库内没有这个编号体系，留着就是一条假引用。

年份（1900~2099）刻意**不**参与数值校验：它是文献元数据、几乎不会编错，
把它算进去只会误删"2017 年提出的 Transformer"这种正常句子。
"""
from __future__ import annotations

import re
from typing import Any

# 行内引用。只认 `[引#N]`（中文/英文方括号都行）——这是本项目 QA/综述一路沿用的写法，
# 模型对它的出错率远低于"把引用编号放进 JSON 数组字段"（见 review/generate.py 的踩坑记录）。
CITE_RE = re.compile(r"[\[［]引#(\d+)[\]］]")
# 模型把字段名写进正文的残留：`refs: [8]`、`refs{6,15}`。
REFS_ECHO_RE = re.compile(r"refs?\s*[:：]?\s*[\[{（(][^\]}）)\n]{0,80}[\]}）)]\s*", re.I)
# 模型脑补的"论文式引用"：`[12]`、`[12, 15]`。库内没有这套编号，必须清掉。
BRACKET_REF_RE = re.compile(r"[\[［]\s*\d{1,3}(?:\s*[,，\-–]\s*\d{1,3})*\s*[\]］]")

# 句子单元 = 到句末标点为止的正文 + 紧跟其后的引用标记。理由见模块 docstring 第 1 条。
_UNIT_RE = re.compile(r"[^。！？!?]*(?:[。！？!?]+|$)(?:[\[［]引#\d+[\]］])*")

# 声称"这是结论"的措辞。命中即判 claim，与模型自己标的 type 无关。
_CLAIM_CUES = (
    "优于",
    "劣于",
    "高于",
    "低于",
    "超过",
    "不如",
    "提升",
    "提高",
    "降低",
    "下降",
    "减少",
    "增加",
    "达到",
    "相当",
    "相差",
    "领先",
    "落后",
    "显著",
    "有效",
    "可行",
    "优于",
    "不如",
    "相比",
    "对比",
    "表明",
    "证明",
    "显示",
    # 评价性形容词同样是断言："表现出色的 Transformer" 就是在下一个没引用的判断。
    # 这类词不加进来，"bridge 免引用"就会变成"形容词免引用"。
    "出色",
    "优异",
    "强大",
    "广泛",
    "成功",
    # 价值判断词同理。"展现出巨大的潜力"没有任何证据，却常被包装成
    # "在此基础上，……"式的承接句——它承接了上一节，但下半句是自己的判断。
    "潜力",
    "前景",
    "价值",
    "关键",
    "核心",
    "趋势",
    "主流",
    "首选",
    "重要",
)

# 过渡句的典型形态。用于给"模型标 bridge 且确实没有结论"的句子一个正面依据，
# 不是为了自动生成 bridge —— bridge 只能由模型写，服务端只做减法。
_BRIDGE_CUES = (
    "因此",
    "因而",
    "在此基础上",
    "由此可见",
    "综上",
    "下面",
    "接下来",
    "首先",
    "其次",
    "最后",
    "此外",
    "另一方面",
    "与之相对",
    "值得注意",
    "需要说明",
)

_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
# 带这些单位的一位数也要核对（见 `quantitative_tokens`）。
_QUANT_UNITS = {"%", "％", "倍", "×", "成"}


def split_units(text: str) -> list[str]:
    return [m.group(0).strip() for m in _UNIT_RE.finditer(text or "") if m.group(0).strip()]


def strip_cites(text: str) -> str:
    return CITE_RE.sub("", text or "").strip()


def clean_sentence(text: str) -> str:
    """清掉引用标记、字段名残留、脑补编号，只留正文；并记录被清掉的东西由调用方去审计。"""
    t = REFS_ECHO_RE.sub("", text or "")
    t = strip_cites(t)
    t = BRACKET_REF_RE.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t.strip(" 　")


def parse_units(text: str) -> list[dict[str, Any]]:
    """
    把一段文字拆成句子单元，每单元带它自己的引用编号。

    返回 `[{"text": "……。", "raw": "……。[引#3]", "cites": [3]}]`。
    `text` 已去掉引用标记（渲染时按 `cites` 重新插，保证编号位置由我们决定）。
    """
    out: list[dict[str, Any]] = []
    for unit in split_units(text):
        cites = [int(x) for x in CITE_RE.findall(unit)]
        body = clean_sentence(unit)
        # `raw` 里保留原始引用串，`dropped_marks` 审计用得上
        if not body:
            continue
        out.append({"text": body, "raw": unit, "cites": cites})
    return out


def quantitative_tokens(text: str) -> list[str]:
    """
    取出"值得核对"的数字：带小数的、带 % / 倍 / ×的、3 位以上的整数。

    年份排除（见模块 docstring 第 4 条）。裸的 1~2 位整数也排除：
    "3 篇文献"这类计数多半是写作时顺手写的，不构成编造风险。

    但 `3 倍` 必须核对——"显存降低 3 倍"是最典型的编造型结论，
    它恰好是一位数，如果只看位数就会被放过。所以量级单位（% 倍 × 成）单独算一类。
    """
    tokens: list[str] = []
    for m in _NUM_RE.finditer(text or ""):
        raw = m.group(0).replace(",", "")
        if not raw:
            continue
        unit = (text[m.end() : m.end() + 1] or "").strip()
        if "." in raw or unit in _QUANT_UNITS:
            tokens.append(raw)
            continue
        if _YEAR_RE.match(raw):
            continue
        if len(raw) >= 3:
            tokens.append(raw)
    # 去重但保序
    seen: set[str] = set()
    uniq = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def _num_variants(token: str) -> list[str]:
    """一个数字在原文里可能的写法：`12.5` / `12.5%` / `12,5`。"""
    out = {token, token.replace(".", ",")}
    return [x for x in out if x]


def numbers_grounded(text: str, evidence_text: str) -> tuple[bool, list[str]]:
    """
    正文里的数字是否都能在**它引的那几段**原文里找到。

    比对是"子串包含"，不做四舍五入容忍：模型改写数字（3.2% → 3.3%）正是要拦的东西，
    容忍度给宽了这道闸就形同虚设。原文里的千分位逗号会先被去掉。
    """
    ev = (evidence_text or "").replace(",", "")
    missing: list[str] = []
    for token in quantitative_tokens(text):
        if any(v in ev for v in _num_variants(token)):
            continue
        missing.append(token)
    return (not missing), missing


def looks_like_claim(text: str) -> bool:
    """文本上是不是在下结论。用于复核模型自标的 bridge。"""
    body = strip_cites(text or "")
    if not body:
        return False
    if quantitative_tokens(body):
        return True
    if any(cue in body for cue in _CLAIM_CUES):
        return True
    # 有引用标记的句子一定在指涉证据，不可能是纯连接句
    return bool(CITE_RE.search(text or ""))


def sentence_type(raw_text: str, declared: str | None) -> tuple[str, str]:
    """
    返回 (最终类型, 改判原因)。

    - 模型标 claim → claim（不给它"标 claim 却能免引用"的口子，引用由后续校验管）；
    - 模型标 bridge → 先看文本像不像结论，像就改判 claim；
    - 模型没标 → 按文本判断。
    """
    declared = (declared or "").strip().lower()
    if declared == "claim":
        return "claim", ""
    if declared == "bridge":
        if looks_like_claim(raw_text):
            return "claim", "bridge_with_claim_cues"
        return "bridge", ""
    return ("claim", "missing_type") if looks_like_claim(raw_text) else ("bridge", "missing_type")


def has_bridge_cue(text: str) -> bool:
    """纯信息用途：这一句是否带典型的连接词（界面据此解释"为什么它被标为无需引用"）。"""
    return any(cue in (text or "") for cue in _BRIDGE_CUES)
