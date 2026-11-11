"""
数值/公式类查询的规则召回表。

为什么单靠融合召回不够：
「β₂」「8×A100」「BLEU 28.4」「10⁻⁹」这类串在语义空间里几乎不携带信息——
dense 看到的是"一个希腊字母加一个数字""一台机器型号"，sparse 只按词频给分，
两边都不知道"用户要的是**这个具体数值出现在哪一段**"。
结果是：正确片段（往往是一个短表格行或一句实验设置）被一段语义相近的散文压下去，
而它恰好是唯一能回答问题的证据。

所以这一层不参与"理解"，只做两件事：
  1. 从问句里抽出**结构化探针**（符号 / 硬件数量 / 指标数值 / 科学计数 / 公式 / 图表编号）；
  2. 用探针在语料里做正则命中，命中的片段（尤其是表格单元格）作为额外候选注入融合结果，
     交给精排去定序。

之所以不把这个逻辑塞进 point 策略：它和"哪类问题"无关，
nav 问"8×A100 出现在哪一页"同样需要它，compare 问"两篇的 β2 差多少"也需要它。
放在召回层，所有策略共享同一张表。

设计上的一个反直觉点：**表格不是强信号，"单元格形态"才是**。
本项目的 PDF 表格抽取质量参差，`kind=table` 的片段里有大量 `' | | | | '` 这种空壳；
真正有内容的是"短行 + 数字占比高 + 用 | 或换行分隔"的片段，
它们可能是 table，也可能是被切成 reference 的表体。所以按形态判，不按 kind 判。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.services.rag.taxonomy import (
    NUMERIC_CELL_MULT,
    NUMERIC_RECALL_LIMIT,
    NUMERIC_REFERENCE_PENALTY,
)

# ---------------------------------------------------------------------------
# 符号与数字的写法归一
# ---------------------------------------------------------------------------

GREEK_GLYPHS: dict[str, str] = {
    "alpha": "α",
    "beta": "β",
    "gamma": "γ",
    "delta": "δ",
    "epsilon": "ϵ",
    "lambda": "λ",
    "mu": "μ",
    "sigma": "σ",
    "eta": "η",
    "theta": "θ",
    "omega": "ω",
    "rho": "ρ",
    "tau": "τ",
}

_SUB_DIGITS = "₀₁₂₃₄₅₆₇₈₉"

# 中文/英文数词：论文正文常写 "eight P100 GPUs"，问句却写 "8×P100"，
# 不归一就会漏掉最该命中的那一句。
_NUMBER_WORDS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12,
    "sixteen": 16, "thirty-two": 32, "sixty-four": 64,
    "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十六": 16,
}

_HW_MODELS = r"(?:A100|A800|H100|H800|V100|P100|T4|TPU\s*v?\d[a-z]?|GPU|TPU)"


# ---------------------------------------------------------------------------
# 探针
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Probe:
    """
    一个从问句里抽出来的"要找的东西"。

    patterns 是**等价写法**的集合，不是单纯的原样转义：
    问句写 β₂、正文写 `β2`、附录写 `\beta_2`，三者必须能互相命中，
    否则规则召回会把"用户问的就是原文里的那句话"判成没找到。
    """

    rule_id: str
    kind: str
    raw: str                      # 问句里的原样
    label: str                    # 归一化后的展示名（trace 用）
    patterns: tuple[str, ...]     # 命中正文的等价正则
    weight: float

    def matches(self, text: str) -> bool:
        for pat in self.patterns:
            if re.search(pat, text, re.IGNORECASE):
                return True
        return False


@dataclass(frozen=True)
class NumericRule:
    """规则召回表的一行。表本身是数据，抽取逻辑在 EXTRACTORS 里。"""

    id: str
    label: str
    kind: str
    weight: float
    extractor: str
    example: str
    note: str


# 规则召回表 —— 本步的"期望输出 ②"，同时通过 /api/v1/rag/numeric-rules 暴露
NUMERIC_RULES: list[NumericRule] = [
    NumericRule(
        id="num-symbol-subscript",
        label="带下标的希腊符号",
        kind="symbol",
        weight=1.00,
        extractor="symbol",
        example="β₂ / β2 / \\beta_2",
        note="β₂ 在 PDF 里可能是 unicode 下标、普通数字后缀或 LaTeX，三种写法都要归一",
    ),
    NumericRule(
        id="num-symbol-bare",
        label="裸希腊符号",
        kind="symbol",
        weight=0.55,
        extractor="symbol",
        example="α / λ / ε",
        note="没有下标时区分度低（α 在论文里到处都是），权重压一半",
    ),
    NumericRule(
        id="num-hardware-count",
        label="硬件数量×型号",
        kind="hardware",
        weight=1.00,
        extractor="hardware",
        example="8×A100 / 8 块 V100 / eight P100 GPUs",
        note="数量与型号必须同时出现；正文写 '8 NVIDIA P100 GPUs' 也要能命中 '8×P100'",
    ),
    NumericRule(
        id="num-hardware-model",
        label="硬件型号",
        kind="hardware",
        weight=0.45,
        extractor="hardware_model",
        example="A100 / V100 / TPU v3",
        note="只有型号、没有数量时，只能说明这段在讲硬件，不足以定位",
    ),
    NumericRule(
        id="num-metric-value",
        label="指标 + 数值",
        kind="metric",
        weight=1.00,
        extractor="metric",
        example="BLEU 28.4 / F1 = 0.87 / accuracy 92.3%",
        note="指标名与数值的间距要收紧到 12 字符，否则会跨列命中表格里的无关数字",
    ),
    NumericRule(
        id="num-scientific",
        label="科学计数法",
        kind="numeric",
        weight=0.85,
        extractor="scientific",
        example="10⁻⁹ / 1e-4 / 10^-9",
        note="ϵ = 10−9 这类写法在 PDF 里常被抽成 '10−9'，上标符号要一起归一到 e 记法",
    ),
    NumericRule(
        id="num-percent",
        label="百分比",
        kind="numeric",
        weight=0.60,
        extractor="percent",
        example="92.3% / 15.4 %",
        note="单独的百分数区分度一般，但配合指标名时极准",
    ),
    NumericRule(
        id="num-range",
        label="区间",
        kind="numeric",
        weight=0.50,
        extractor="range",
        example="64~512 / 0.8–1.2",
        note="区间在超参搜索空间里很常见，单独出现时权重较低",
    ),
    NumericRule(
        id="num-formula-complexity",
        label="复杂度/公式记号",
        kind="formula",
        weight=0.80,
        extractor="formula",
        example="O(n²) / \\frac / \\sum",
        note="复杂度记号是公式类查询最直接的锚点",
    ),
    NumericRule(
        id="num-chart-ref",
        label="图表/公式编号",
        kind="chart",
        weight=0.35,
        extractor="chart_ref",
        example="Table 3 / Figure 2 / 图 4",
        note="编号本身很弱（满篇都是 'Table 1'），只作为「这段是不是目标图表」的辅助信号",
    ),
]


RULE_BY_ID: dict[str, NumericRule] = {r.id: r for r in NUMERIC_RULES}
RULE_BY_EXTRACTOR: dict[str, NumericRule] = {r.extractor: r for r in NUMERIC_RULES}


# ---------------------------------------------------------------------------
# 抽取器：问句 → 探针
# ---------------------------------------------------------------------------

def _probe(
    extractor: str, raw: str, label: str, patterns: tuple[str, ...], rule_id: str | None = None
) -> Probe:
    """
    构造一个探针。

    rule_id 必须能覆盖"同一抽取器对应多条规则"的情况：
    符号抽取器同时服务 `num-symbol-subscript`（1.00）与 `num-symbol-bare`（0.55），
    只按 extractor 查表会永远拿到后者——β₂ 被当成裸 β，权重白白砍半。
    """
    rule = RULE_BY_ID[rule_id] if rule_id else RULE_BY_EXTRACTOR[extractor]
    return Probe(
        rule_id=rule.id,
        kind=rule.kind,
        raw=raw,
        label=label,
        patterns=patterns,
        weight=rule.weight,
    )


# 希腊字母后面可能跟：unicode 下标（β₂）、ASCII 数字（β2）、下标记号（β_2）。
# 三种都要捕到，否则 "β2 设为多少" 会被当成"裸 β"处理，权重从 1.0 掉到 0.55。
_SYMBOL_GREEK_RE = re.compile(
    r"([α-ωϵΑ-Ω])\s*(?:[_^]\s*)?([" + _SUB_DIGITS + r"0-9])?"
)
# 名字写法（beta2 / beta_2 / \beta_2）。数字必须"贴着"名字或带 _ / ^，
# 不能允许"名字 + 空格 + 数字"：否则 "epsilon 1e-9" 会被抽成 epsilon1（下标 1），
# 拿一个不存在的符号去扫全文。
# 注意这里用 (?![a-z]) 而不是 \b：_ 也算 word 字符，"beta_2" 在 "beta" 与 "_" 之间
# 没有 word 边界，用 \b 会把带下标的写法整条漏掉。
_SYMBOL_NAME_RE = re.compile(
    r"\b(" + "|".join(GREEK_GLYPHS) + r")(?![a-z])(?:[_^]\s*\{?([0-9])\}?|([0-9])\b)?",
    re.IGNORECASE,
)
_SYMBOL_LATEX_RE = re.compile(
    r"\\(alpha|beta|gamma|delta|epsilon|lambda|mu|sigma|eta|theta|omega)(?![a-z])(?:_\s*\{?([0-9])\}?)?"
)


def _symbol_patterns(name: str, digit: str | None) -> tuple[str, ...]:
    glyph = GREEK_GLYPHS.get(name, name)
    if not digit:
        return (
            re.escape(glyph),
            rf"\b{name}\b",
            rf"\\{name}\b",
        )
    sub = _SUB_DIGITS[int(digit)]
    return (
        rf"{re.escape(glyph)}\s*[_^]?\s*\{{?{digit}\}}?",   # β2 / β_2 / β^{2}
        rf"{re.escape(glyph)}{sub}",                         # β₂
        rf"\b{name}\s*[_^]?\s*\{{?{digit}\}}?\b",            # beta2 / beta_2
        rf"\\{name}\s*[_^]?\s*\{{?{digit}\}}?",              # \beta_2
    )


def _extract_symbol(question: str) -> list[Probe]:
    out: list[Probe] = []
    seen: set[str] = set()
    for m in _SYMBOL_GREEK_RE.finditer(question):
        glyph = m.group(1)
        name = next((k for k, v in GREEK_GLYPHS.items() if v == glyph), None)
        if not name:
            continue
        digit = None
        tail = (m.group(2) or "").strip()
        if tail:
            digit = str(_SUB_DIGITS.index(tail)) if tail in _SUB_DIGITS else tail
        label = f"{name}{digit or ''}"
        if label in seen:
            continue
        seen.add(label)
        out.append(_probe(
            "symbol", m.group(0), label, _symbol_patterns(name, digit),
            rule_id="num-symbol-subscript" if digit else "num-symbol-bare",
        ))

    for rx in (_SYMBOL_NAME_RE, _SYMBOL_LATEX_RE):
        for m in rx.finditer(question):
            name = m.group(1).lower()
            # 前一支是"带 _ / ^ 的下标"，后一支是"数字紧跟名字"（beta2）
            digit = m.group(2) or (m.group(3) if rx.groups >= 3 else None)
            label = f"{name}{digit or ''}"
            if label in seen:
                continue
            seen.add(label)
            out.append(_probe(
                "symbol", m.group(0), label, _symbol_patterns(name, digit),
                rule_id="num-symbol-subscript" if digit else "num-symbol-bare",
            ))
    return out


_HW_RE = re.compile(
    rf"(?P<count>\d{{1,3}}|" + "|".join(_NUMBER_WORDS) + rf")\s*(?:块|张|台|个)?\s*"
    rf"(?:[×xX*]\s*)?(?:NVIDIA\s+)?(?P<model>{_HW_MODELS})(?:s\b|es\b)?",
    re.IGNORECASE,
)
_HW_REV_RE = re.compile(
    rf"(?P<model>{_HW_MODELS})\s*[×xX*]\s*(?P<count>\d{{1,3}})", re.IGNORECASE
)
_HW_MODEL_ONLY_RE = re.compile(rf"\b({_HW_MODELS})\b", re.IGNORECASE)


def _count_value(token: str) -> int | None:
    low = token.lower()
    if low.isdigit():
        return int(low)
    return _NUMBER_WORDS.get(low)


def _hardware_patterns(count: int, model: str) -> tuple[str, ...]:
    m = re.escape(model)
    return (
        rf"{count}\s*(?:块|张|台|个)?\s*(?:[×xX*]\s*)?(?:NVIDIA\s+)?{m}",
        rf"{m}\s*(?:s|es)?\s*[×xX*]\s*{count}",
    )


def _extract_hardware(question: str) -> list[Probe]:
    out: list[Probe] = []
    seen: set[str] = set()
    for m in _HW_RE.finditer(question):
        count = _count_value(m.group("count"))
        model = m.group("model").strip()
        if not count or count <= 1:
            continue
        # "8 GPUs" 没有型号，不算数量×型号探针，交给型号规则
        if model.upper() in ("GPU", "TPU"):
            continue
        label = f"{count}×{model.upper()}"
        if label in seen:
            continue
        seen.add(label)
        out.append(_probe("hardware", m.group(0).strip(), label, _hardware_patterns(count, model)))

    for m in _HW_REV_RE.finditer(question):
        model = m.group("model")
        count = int(m.group("count"))
        label = f"{count}×{model.upper()}"
        if label in seen:
            continue
        seen.add(label)
        out.append(_probe("hardware", m.group(0).strip(), label, _hardware_patterns(count, model)))
    return out


def _extract_hardware_model(question: str) -> list[Probe]:
    out: list[Probe] = []
    seen: set[str] = set()
    for m in _HW_MODEL_ONLY_RE.finditer(question):
        model = m.group(1).strip()
        if model.upper() in ("GPU", "TPU"):
            continue
        label = model.upper()
        if label in seen:
            continue
        seen.add(label)
        out.append(_probe("hardware_model", m.group(0), label, (rf"\b{re.escape(model)}\b",)))
    return out


_METRIC_WORDS = (
    r"(?:BLEU(?:-?\d+)?|ROUGE-?[12Ll]|F1|F-1|accuracy|acc\.?|perplexity|PPL|"
    r"mAP|EM|WER|MSE|RMSE|Spearman|Pearson|top-?1|top-?5)"
)
_METRIC_RE = re.compile(
    rf"(?P<metric>{_METRIC_WORDS})\s*(?:score|scores|of|:|：|=|＝|达到|为|是)?\s*"
    rf"(?P<value>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_METRIC_REV_RE = re.compile(
    rf"(?P<value>\d+(?:\.\d+)?)\s*(?:points?|个点)?\s*(?P<metric>{_METRIC_WORDS})",
    re.IGNORECASE,
)


def _metric_patterns(metric: str, value: str) -> tuple[str, ...]:
    m = re.escape(metric)
    v = re.escape(value)
    return (
        rf"{m}[^\d\n]{{0,12}}{v}",
        rf"{v}[^\d\n]{{0,12}}{m}",
    )


def _extract_metric(question: str) -> list[Probe]:
    out: list[Probe] = []
    seen: set[str] = set()
    for rx in (_METRIC_RE, _METRIC_REV_RE):
        for m in rx.finditer(question):
            metric = m.group("metric").upper()
            value = m.group("value")
            label = f"{metric} {value}"
            if label in seen:
                continue
            seen.add(label)
            out.append(_probe("metric", m.group(0).strip(), label, _metric_patterns(metric, value)))
    return out


_SCI_RE = re.compile(
    # 前一支必须带幂记号（^ / 上标 ⁻ / ** / 负号），否则 "A100" 里的 "100" 会被
    # 当成 10 的 0 次幂，凭空造出一个探针去扫 "10-0" 这种串。
    r"10\s*(?:\^|⁻|\*\*|[−-])\s*[−-]?\s*([0-9]{1,2})(?=\D|$)|(\d+(?:\.\d+)?)\s*[eE]\s*([−-]?\d{1,2})"
)


def _extract_scientific(question: str) -> list[Probe]:
    out: list[Probe] = []
    seen: set[str] = set()
    for m in _SCI_RE.finditer(question):
        raw = m.group(0).strip()
        if m.group(1) is not None:
            exp = m.group(1)
            # 10 的负幂：幂符号在这三种写法里都可能出现
            patterns = (
                rf"10\s*(?:\^|⁻|\*\*)?\s*[−-]?\s*{exp}\b",
                rf"1\s*[eE]\s*[−-]\s*{exp}\b",
                rf"10\s*[−-]\s*{exp}\b",
            )
            label = f"10^-{exp}"
        else:
            exp = m.group(3)
            patterns = (rf"1\s*[eE]\s*[−-]?\s*{exp.lstrip('−-')}\b",)
            label = f"1e{exp}"
        if label in seen:
            continue
        seen.add(label)
        out.append(_probe("scientific", raw, label, patterns))
    return out


def _extract_percent(question: str) -> list[Probe]:
    out: list[Probe] = []
    seen: set[str] = set()
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*%", question):
        value = m.group(1)
        label = f"{value}%"
        if label in seen:
            continue
        seen.add(label)
        out.append(_probe("percent", m.group(0), label, (rf"{re.escape(value)}\s*%",)))
    return out


def _extract_range(question: str) -> list[Probe]:
    out: list[Probe] = []
    seen: set[str] = set()
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*[~–—-]\s*(\d+(?:\.\d+)?)", question):
        a, b = m.group(1), m.group(2)
        # 年份区间（2014-2016）不是超参区间，跳过
        if len(a) == 4 and len(b) == 4 and a.startswith("20"):
            continue
        label = f"{a}~{b}"
        if label in seen:
            continue
        seen.add(label)
        out.append(_probe("range", m.group(0), label, (rf"{re.escape(a)}\s*[~–—-]\s*{re.escape(b)}",)))
    return out


_FORMULA_RES = [
    (re.compile(r"O\s*\(\s*[^)]{1,24}\)"), "big-O"),
    (re.compile(r"\\[a-zA-Z]{2,}"), "latex"),
    (re.compile(r"[∑∏∫√]"), "math-symbol"),
]


def _extract_formula(question: str) -> list[Probe]:
    out: list[Probe] = []
    seen: set[str] = set()
    for rx, tag in _FORMULA_RES:
        for m in rx.finditer(question):
            raw = m.group(0).strip()
            if tag == "latex":
                # \beta_2 已由符号规则处理，避免重复
                if raw.lower() in {f"\\{n}" for n in GREEK_GLYPHS}:
                    continue
                label = raw
                pats = (re.escape(raw),)
            elif tag == "big-O":
                inner = raw[raw.find("(") + 1: raw.rfind(")")].strip()
                label = raw
                pats = (rf"O\s*\(\s*{re.escape(inner)}\s*\)",)
            else:
                label = raw
                pats = (re.escape(raw),)
            if label in seen:
                continue
            seen.add(label)
            out.append(_probe("formula", raw, label, pats))
    return out


def _extract_chart_ref(question: str) -> list[Probe]:
    out: list[Probe] = []
    seen: set[str] = set()
    rx = re.compile(r"(table|tab\.|figure|fig\.|表|图|公式|式)\s*[\(（]?\s*(\d+)", re.IGNORECASE)
    for m in rx.finditer(question):
        num = m.group(2)
        label = f"{m.group(1).lower()} {num}"
        if label in seen:
            continue
        seen.add(label)
        out.append(_probe(
            "chart_ref", m.group(0), label,
            (rf"(?:table|tab\.?|figure|fig\.?|表|图|公式|式)\s*[\(（]?\s*{num}\b",),
        ))
    return out


EXTRACTORS: dict[str, Callable[[str], list[Probe]]] = {
    "symbol": _extract_symbol,
    "hardware": _extract_hardware,
    "hardware_model": _extract_hardware_model,
    "metric": _extract_metric,
    "scientific": _extract_scientific,
    "percent": _extract_percent,
    "range": _extract_range,
    "formula": _extract_formula,
    "chart_ref": _extract_chart_ref,
}


def scan_query(question: str) -> list[Probe]:
    """
    抽出问句里所有可用的结构化探针。

    顺序就是可信度顺序：符号 > 硬件数量 > 指标数值 > 科学计数 > …，
    同名探针只保留第一次出现的（信息量最高、权重也最高）。
    """
    probes: list[Probe] = []
    seen: set[str] = set()
    for rule in NUMERIC_RULES:
        fn = EXTRACTORS.get(rule.extractor)
        if not fn:
            continue
        for p in fn(question):
            key = f"{p.kind}:{p.label}"
            if key in seen:
                continue
            seen.add(key)
            probes.append(p)
    return probes


# ---------------------------------------------------------------------------
# 命中打分
# ---------------------------------------------------------------------------

def is_cell_like(text: str) -> bool:
    """
    判断片段是不是"表格单元格形态"。

    不按 kind 判，是因为本项目的表格抽取质量不稳定：
    `kind=table` 里有整段 `' | | | | '` 的空壳，也有被切成 reference 的表体。
    三种形态特征里命中任一即可：
      - 有竖线分隔（表格线残留）
      - 多行且数字占比高（一行一个数）
      - 极短（≤40 字符）且含数字（单个单元格）
    """
    if not text:
        return False
    if text.count("|") >= 2:
        return True
    lines = [ln for ln in text.splitlines() if ln.strip()]
    digits = sum(ch.isdigit() for ch in text)
    if len(lines) >= 3 and digits / max(1, len(text)) >= 0.10:
        return True
    if len(text) <= 40 and digits:
        return True
    return False


def score_chunk(text: str, kind: str, probes: list[Probe]) -> tuple[float, list[str]]:
    """
    一条片段针对这组探针的规则分。

    为什么要乘 CELL_MULT 而不是"表格加固定分"：
    用户找的是"8×A100 这个事实出现在哪"，而一个事实在语料里往往以单元格形态出现。
    形态是**相关性证据**，不是**类型偏好**——加固定分会让所有表格一起上浮，
    乘系数只放大"真的命中了探针的那几条"。
    """
    if not text or not probes:
        return 0.0, []
    cell = is_cell_like(text)
    boost = 0.0
    hits: list[str] = []
    for p in probes:
        if not p.matches(text):
            continue
        w = p.weight * (NUMERIC_CELL_MULT if cell else 1.0)
        boost += w
        hits.append(p.label)
    if (kind or "").lower() == "reference":
        # 参考文献片段里同样会出现这些串，但它们是引用别人的数据，不是本文的结论
        boost *= max(0.0, 1.0 - NUMERIC_REFERENCE_PENALTY)
    return round(boost, 4), hits


@dataclass
class NumericRecall:
    """规则召回的一次结果，整体进 trace。"""

    hits: list[Any] = field(default_factory=list)
    probes: list[Probe] = field(default_factory=list)
    scanned: int = 0
    matched: int = 0
    table_cells: int = 0
    latency_ms: float = 0.0
    scope: list[str] = field(default_factory=list)   # 限定的论文范围（空 = 全语料）
    out_of_scope: int = 0                            # 因不在范围内被跳过的片段数

    @property
    def used(self) -> bool:
        return bool(self.hits)

    def to_trace(self) -> dict[str, Any]:
        return {
            "used": self.used,
            "probes": [{"label": p.label, "rule": p.rule_id, "weight": p.weight} for p in self.probes],
            "scanned": self.scanned,
            "matched": self.matched,
            "table_cells": self.table_cells,
            "injected": len(self.hits),
            "paper_scope": self.scope,
            "out_of_scope": self.out_of_scope,
            "latency_ms": self.latency_ms,
            "top": [
                {
                    "chunk_id": h.chunk_id,
                    "arxiv_id": h.arxiv_id,
                    "page": h.page,
                    "kind": h.kind,
                    "score": h.score,
                    "cell": is_cell_like(h.text or ""),
                    "hits": (h.hit_source or "").replace("rule:", ""),
                }
                for h in self.hits[:5]
            ],
        }


def describe_rules() -> list[dict[str, Any]]:
    """给 /api/v1/rag/numeric-rules 与文档用的表格视图。"""
    return [
        {
            "id": r.id,
            "label": r.label,
            "kind": r.kind,
            "weight": r.weight,
            "example": r.example,
            "note": r.note,
        }
        for r in NUMERIC_RULES
    ]


# ---------------------------------------------------------------------------
# 规则召回：在语料里找"含有这个探针"的片段
# ---------------------------------------------------------------------------

def recall(
    corpus: dict[str, Any],
    probes: list[Probe],
    limit: int = NUMERIC_RECALL_LIMIT,
    paper_ids: set[str] | None = None,
) -> NumericRecall:
    """
    用探针扫全语料，返回命中的片段（按规则分排序、截断到 limit）。

    为什么是"扫全语料"而不是"给融合候选重排"：
    数值类问题的答案常常只出现在**一个孤立的单元格**里，那段文本没有任何语义上下文，
    dense 和 sparse 都不会把它召回——不是排得靠后，是压根进不了候选池。
    这时重排无能为力，只能靠字面扫描把它捞出来。
    代价是 O(片段数 × 探针数) 的正则匹配，但语料是几百段、探针是个位数，
    实测 285 段 × 3 探针约 2ms，比一次 embedding 便宜得多。

    paper_ids：**论文范围**，通常是融合候选里已经出现过的 arxiv_id。
    为什么需要它：符号是多义的。「β2 设为多少」在 Transformer 论文里是 0.98，
    在光纤孤子论文里是 −20 ps/nm/km——两条都"字面命中 β2"，规则分一样高。
    规则层没有语义能力判断用户问的是哪一篇，但融合候选有：
    它已经把用户意图指向的论文带进来了。所以规则召回只在这些论文内部找那个孤立单元格，
    跨论文引入新证据的权力留给融合（它才是负责"找哪几篇"的那一层）。
    实测：不设范围时一条光纤论文的 β2 会被注入到第 2 位，最终答案从
    「β2 = 0.98」变成「β2 = −20 ps/nm/km」（见 docs/2.9 第六节）。
    """
    from app.services.rag.strategies import _payload_to_result  # 延迟导入：避免模块级循环

    started = time.perf_counter()
    out = NumericRecall(probes=probes)
    if not probes or not corpus.get("chunks"):
        return out
    out.scope = sorted(paper_ids) if paper_ids else []

    scored: list[tuple[float, list[str], dict[str, Any], str]] = []
    for rec in corpus["chunks"]:
        pl = rec["payload"]
        if paper_ids is not None and pl.get("arxiv_id") not in paper_ids:
            out.out_of_scope += 1
            continue
        text = " ".join(
            x for x in ((pl.get("text") or ""), (pl.get("caption") or ""), (pl.get("description") or "")) if x
        )
        if not text:
            continue
        out.scanned += 1
        boost, hits = score_chunk(text, pl.get("kind") or "", probes)
        if boost <= 0:
            continue
        out.matched += 1
        if is_cell_like(pl.get("text") or ""):
            out.table_cells += 1
        scored.append((boost, hits, pl, rec["id"]))

    scored.sort(key=lambda x: x[0], reverse=True)
    # 逐条材料化成 RetrievalResult，score 用规则分的相对值（0~1），
    # 让"规则召回"和"融合归一化分"在同一量级上比较，注入后不会一边倒。
    top_boost = scored[0][0] if scored else 1.0
    for boost, hits, pl, cid in scored[:limit]:
        res = _payload_to_result(pl, cid, score=round(boost / (top_boost or 1.0), 4))
        res.raw_score = None          # 没有融合分，不参与 RRF 拒答判定
        res.hit_source = "rule:" + ",".join(hits[:3])
        out.hits.append(res)

    out.latency_ms = round((time.perf_counter() - started) * 1000, 1)
    return out


__all__ = [
    "NUMERIC_RULES",
    "NumericRecall",
    "NumericRule",
    "Probe",
    "describe_rules",
    "is_cell_like",
    "recall",
    "scan_query",
    "score_chunk",
]
