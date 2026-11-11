"""
综述生成的三个 prompt（带版本号）。

为什么综述的 prompt 要拆成三段而不是"一段话让模型写综述"：
一段式生成的结果必然是**摘要拼接**——模型按检索顺序把每段片段复述一遍，
遇到相反结论就写"研究表明存在争议"把它抹平。这是本任务明令禁止的形态。
拆成「抽 claim → 判冲突 → 按簇写」之后，簇结构是服务端算出来的中间表示，
模型只负责在给定簇内把话说顺，它**没有"合并两个簇"的输出位置**，
所以"相反结论不得合并"这件事由结构保证，而不是靠 prompt 里求它别合并。

版本约定：
- 三个 prompt 是一个整体，任何一处改动都必须同时 bump `REVIEW_PROMPT_VERSION`；
- `config_block()` 把版本号 + prompt 文本指纹 + 阈值一起回显到响应体，
  复盘时能确认"这份综述到底是按哪版 prompt、哪套阈值写的"。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from app.core.config import settings

REVIEW_PROMPT_VERSION = "review-v1.2.0"

# ---------------------------------------------------------------- 1. 抽 claim

CLAIM_EXTRACT_SYSTEM = """你是学术证据抽取器。你的任务是从编号片段中抽取「可判真假的结论」，
而不是复述段落。你只输出 JSON，不解释。

对每条结论，必须同时给出四个字段，它们决定了后续能否正确聚簇与判冲突：

1. `axis`（比较轴）：把这条结论所在的那个"可判定维度"写成一个固定格式的短标签，
   格式为 `对象A 相对 对象B：指标` 或 `主题：维度`。例如
   `LoRA 相对 全量微调：下游任务性能`、`ViT 相对 ResNet：ImageNet 准确率`、
   `Transformer：训练并行度`。
   **同一个维度必须每次用完全相同的字面写法**，这是聚簇的主键，所以：
   - 对象用**通用模型名**，不要带论文内部的变体编号（`ViT-L/16`、`BERT_LARGE` 一律写成 `ViT`、`BERT`）；
   - 指标用**通用名**，不要写具体数值或数据集子集（`ImageNet 准确率`，不要写 `ImageNet-21k top-1 85.3`）；
   - 不同指标不许合并（准确率 ≠ 训练速度 ≠ 显存占用）；
   - **轴必须描述这条结论自己在判断什么，不是这篇论文在研究什么**。
     描述架构/流程的结论，轴写 `对象：方法设计`；描述结果的结论，轴写 `对象A 相对 对象B：指标`。
     把"模型把图像切成 16×16 的块"这种结构描述打上"ImageNet 准确率"的轴，会让它和
     真正比准确率的结论聚成一簇，这是本任务最隐蔽的错误来源。
   - **优先对齐综述主题问的那个问题**。如果片段能直接回答主题（例如主题问「X 是否优于 Y」），
     就统一用 `X 相对 Y：<指标>` 这种与问法一致的写法，不要为每篇论文各造一个新维度——
     否则本该聚成一个"多篇一致"的簇会散成一地"仅单篇支持"，综述的证据强度会被系统性低估。

2. `polarity`（极性）：沿该 axis 的方向，取自下面三个值之一：
   - `support`   ：片段主张「A 在该维度上优于/不亚于/支持 B」；
   - `oppose`    ：片段主张「A 在该维度上劣于/不支持 B」；
   - `conditional`：片段主张「只有在……条件下才成立」，条件写进 `condition`。
   极性是**相对 axis 的方向**，不是情感色彩。同一 axis 下出现 support 与 oppose，
   就是一对真实的互斥结论，必须如实保留，禁止为了"看起来一致"把其中一条改写成中性。

3. `condition`：该结论成立的前提（数据集规模、模型规模、任务类型、语言……）。
   没有前提就输出空字符串。**有前提的结论必须标 conditional，不能标 support。**

4. `quote`：支撑该结论的原文句子，从片段里**逐字复制**（英文保持英文），不要改写、不要翻译。
   必须是完整的一句话；一句话里含多个并列结论时，拆成多条 claim，各自引用同一句。

另外两个字段：
- `statement`：用中文把这条结论写成一句独立可读的话（会被直接放进综述正文的标题行）；
- `subject`：被断言的主要对象（如 `LoRA`、`ViT-B/16`、`Transformer`）。

禁止事项：禁止在 `quote` 里写片段之外的句子；禁止跨片段拼接引用；
禁止把两条方向相反的结论合成一条（宁可多抽一条，也不要归一化）。"""


def claim_extract_prompt(topic: str, numbered_chunks: str, k: int) -> str:
    return (
        f"综述主题：{topic}\n\n"
        f"以下是编号证据片段（共 {k} 段）：\n\n"
        f"{numbered_chunks}\n\n"
        "请抽取其中所有能支撑综述的结论，输出严格 JSON：\n"
        '{"claims":[{"statement":"","subject":"","axis":"","polarity":"support|oppose|conditional",'
        '"condition":"","quote":"","chunk_index":1}]}\n'
        "要求：\n"
        f"- 最多 {k * 2} 条，只抽原文真的说了的结论，不要补常识；\n"
        "- 每个片段至少抽 1 条；含多个独立结论的片段（Results、Related Work、Limitations）\n"
        "  要逐条列出，不要概括成一条——**被概括掉的往往正是那个反例**；\n"
        "- **反向与限定结论必须优先抽**：出现「不如／更差／落后／few percentage points below／\n"
        "  仅在……时成立／在较小数据集上／diminishing returns」这类表述时，必须单独抽成一条\n"
        "  `oppose` 或 `conditional` 的结论。漏掉它们，综述就只会有一面之词——\n"
        "  这类句子正是后续「冲突标注」唯一的来源；\n"
        "- `chunk_index` 必须是上面片段编号（整数），`quote` 必须出自该编号片段；\n"
        "- 数据/指标类结论必须保留具体数字；\n"
        "- 一条结论若只在特定条件下成立，必须给 `condition` 且 `polarity=conditional`；\n"
        "- 同一 axis 的不同方向结论请全部输出（这正是后续要标注的冲突）。"
    )


# ------------------------------------------------------- 1.5 轴归一化

CANONICALIZE_SYSTEM = """你是观点轴归一化器。输入是若干条"比较轴"的原始写法，
它们来自不同论文、由不同批次抽取，措辞必然不一致（`ViT 相对 BiT ResNet：ImageNet 准确率`
和 `ViT 相对 ResNet：ImageNet 准确率` 说的是同一个维度）。你的任务是把它们归并成标准轴。

归并规则（严格）：
- **只有同一个可判定维度才能合并**。"准确率"与"训练速度"、"显存占用"不是同一个维度，绝不合并；
- **对象不同不合并**：`LoRA 相对 全量微调` 与 `LoRA 相对 Adapter` 是两个轴；
- 只有当两条轴的**对象相同、指标相同**、仅措辞/粒度不同时才合并；
- 合并后的标准轴用最通用、最短的写法；`members` 里必须列出所有被并入的原始写法（逐字复制）。

宁可多留几个轴，也不要把不同维度并在一起——错误合并会让两条本不相干的结论变成"同一观点"，
进而把综述写错。只输出 JSON。"""


def canonicalize_prompt(axes: list[str]) -> str:
    listing = "\n".join(f"{i}. {a}" for i, a in enumerate(axes, 1))
    return (
        "以下是抽取阶段得到的比较轴原始写法：\n\n"
        f"{listing}\n\n"
        "请归并成标准轴，输出严格 JSON：\n"
        '{"axes":[{"canonical":"","members":["原始写法1","原始写法2"]}]}\n'
        "要求：\n"
        "- 每个原始写法必须且只能出现在一个 `members` 里，不得遗漏、不得重复；\n"
        "- `members` 里的字符串必须与上面逐字一致；\n"
        "- 只保留真正需要合并的分组即可，单独成一个轴的也照常输出（members 只有一个元素）。"
    )


# ------------------------------------------------------------- 2. 判冲突

CONFLICT_VERIFY_SYSTEM = """你是学术分歧审查员。给定若干「同轴但结论相反」的观点对，
判断它们是否构成真实分歧，并给出分歧类型。你只输出 JSON。

判定规则：
- `direct_contradiction`：两个结论在**同一个条件范围内**互相否定，不可能同时为真。
  例如「A 在该数据集上优于 B」vs「A 在同一数据集上劣于 B」。
- `conditional_split`：两条结论各自在自己声明的条件下成立，冲突来自**条件不同**
  （数据规模、模型规模、任务/语言、评测指标不同）。这种情况仍然是需要向读者标注的分歧，
  `confirmed` 也必须为 true——它回答的是"为什么文献说法不一样"，不是"谁错了"。
- `scope_mismatch`：看似相反，其实比较对象/指标/任务根本不是同一个（`confirmed=false`）。
  宁可判 scope_mismatch，也不要把"两个不同问题的答案"硬说成矛盾。
  典型例子：一条在讲**方法结构**（"把图像切块后线性嵌入"），另一条在讲**性能对比**
  （"小数据下 ResNet 更好"）——它们不是一对矛盾，请判 scope_mismatch。

要求：`reason` 用一句中文说明判据，必须点名具体条件或指标；禁止写"存在争议"这种空话。"""


def conflict_verify_prompt(pairs: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for p in pairs:
        lines.append(
            f"{p['pair_id']}:\n"
            f"  axis: {p['axis']}\n"
            f"  A（{p['a']['stance']}，{p['a']['paper_count']} 篇）：{p['a']['statement']}\n"
            f"     A 的条件：{p['a']['condition'] or '（无）'}\n"
            f"  B（{p['b']['stance']}，{p['b']['paper_count']} 篇）：{p['b']['statement']}\n"
            f"     B 的条件：{p['b']['condition'] or '（无）'}"
        )
    return (
        "请审查以下观点对是否构成真实分歧：\n\n"
        + "\n\n".join(lines)
        + "\n\n输出严格 JSON：\n"
        '{"verdicts":[{"pair_id":"P1","confirmed":true,'
        '"type":"direct_contradiction|conditional_split|scope_mismatch","reason":""}]}\n'
        "每个 pair_id 都必须给出一条判定，不得遗漏。"
    )


# ------------------------------------------------------------- 3. 写综述

REVIEW_WRITE_SYSTEM = """你是学术综述写作助手。你不是在总结检索结果，
而是在**按已给定的观点簇结构**把综述写出来。簇结构由系统预先算好，
你的职责只有一个：为每个簇写清楚「这条结论说了什么、由哪些证据支撑」。

铁律（违反即整段作废）：
1. **每句必须以 `[引#N]` 结尾**（中文方括号 + 引 + # + 编号），例如：
   「LoRA 的推理延迟低于全量微调。[引#2]」一句话可以带多个：`…更低。[引#2][引#5]`
   没有 `[引#N]` 的句子会被系统**整句删除**，等于这一段没写；
   也**不要**写成 `refs: [2]`、`[2]`、`(引#2)`——只认 `[引#N]`。
2. 一个 section 只写一个簇。**禁止**把两个簇的观点写进同一段，
   尤其禁止用「研究表明」「普遍认为」「存在争议」把方向相反的结论含混带过。
3. 只使用该簇证据里的信息。禁止引入证据外的知识、禁止编造数字。
   数字、指标、数据集名照抄证据原文，不要换算、不要四舍五入。
4. 不要复述同一句证据两遍；一段 2~4 句即可，宁短勿长。
5. 不要写标题、不要写"综上所述"、不要写立场判断（立场由系统渲染）。
6. 输出是 JSON，`text` 是一个字符串（整段），不是数组。
7. 即使某个簇只有一条证据，也要写满一句并带上那个编号——**不要因为"没什么可说"就交白卷**。"""


def review_write_prompt(topic: str, clusters_text: str, conflicts_text: str) -> str:
    return (
        f"综述主题：{topic}\n\n"
        f"## 观点簇（每个簇必须且只能产出一个 section）\n\n{clusters_text}\n\n"
        f"## 系统已确认的冲突对（只作为背景，不要在正文里合并它们）\n\n{conflicts_text}\n\n"
        "请按簇写出正文，输出严格 JSON：\n"
        '{"sections":[{"cluster_id":"1","text":"第一句。[引#3]第二句。[引#5][引#3]"}]}\n'
        "要求：\n"
        "- `cluster_id` 必须与上面给的完全一致，每个簇恰好一个 section，顺序不变；\n"
        "- 每句末尾都要有 `[引#N]`，编号必须出现在该簇的证据清单里；\n"
        "- 句子用中文，专业术语保留英文原词；\n"
        "- 若某个簇的证据不足以写出完整句子，就给 1 句最保守的陈述，不要编。"
    )


# ------------------------------------------------------------- 版本与配置

def prompts_fingerprint() -> str:
    """三个 prompt 的文本身份。改一个字指纹就变，用来发现"改了 prompt 忘 bump 版本"。"""
    blob = json.dumps(
        [
            REVIEW_PROMPT_VERSION,
            CLAIM_EXTRACT_SYSTEM,
            CANONICALIZE_SYSTEM,
            CONFLICT_VERIFY_SYSTEM,
            REVIEW_WRITE_SYSTEM,
        ],
        ensure_ascii=False,
    )
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:10]


THRESHOLD_KEYS = (
    "review_pool_per_paper",
    "review_pool_max_chunks",
    "review_subqueries",
    "review_claim_batch",
    "review_axis_merge_sim",
    "review_statement_split_sim",
    "review_conflict_axis_sim",
    "review_conflict_statement_sim",
    "review_conflict_llm_pairs",
)


def thresholds() -> dict[str, Any]:
    return {k: getattr(settings, k) for k in THRESHOLD_KEYS}


def config_block() -> dict[str, Any]:
    """
    响应体里回显的"这份综述是按什么口径生成的"。

    为什么带两个版本号：`prompt_version` 管文本，`version` 管阈值。
    两者经常不同步改动——调阈值不改 prompt 才是常态——所以必须分开记，
    只留一个号就会出现"版本号没变但结果全变了"的不可解释状态。
    """
    payload = json.dumps(
        {"th": thresholds(), "v": settings.review_version}, ensure_ascii=False, sort_keys=True
    )
    return {
        "version": settings.review_version,
        "prompt_version": REVIEW_PROMPT_VERSION,
        "fingerprint": hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10],
        "thresholds": thresholds(),
    }
