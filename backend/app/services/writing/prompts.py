"""
写作辅助的两套 prompt（带版本号）。

为什么写作不能"给个主题写一段"：
那样写出来的每一句都**没有身份证**。读者看到"XX 方法在长文本上更稳定"，
无从知道这句是某篇论文的原文、还是模型把三篇揉在一起之后的印象；
导出成 Word 之后更是连编号都没有了。本任务要求"写出去每一句都可追溯"，
所以句子必须是一个**结构体**，而不是一段字符串：

    {id, section, type: claim|bridge, text, cite_nos:[引#N], chunk_ids:[...]}

模型只负责两件事：
1. 在给定证据里挑出能支撑本段的那几句，用中文把话说顺；
2. 老实标明哪些句子是**过渡/连接句**（不需要引用），哪些是**要下判断的结论句**（必须有引用）。

其余全部由服务端算：编号、chunk 绑定、References、占位、导出映射。
模型没有"编一条参考文献"的输出位置——它连 References 都不写。

版本约定与综述一致：这两套 prompt 是一个整体，任何改动都要 bump
`WRITING_PROMPT_VERSION`；`config_block()` 把版本 + 文本指纹 + 阈值一起回显。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from app.core.config import settings

WRITING_PROMPT_VERSION = "writing-v1.2.0"

# ------------------------------------------------------------- 1. 分段计划

SECTION_PLAN_SYSTEM = """你是学术写作的结构规划器。给定一个主题和库内可用的文献清单，
你要规划出一份**能被现有证据支撑**的短稿结构（3~5 段）。

铁律：
1. **不许规划证据覆盖不到的部分**。如果库里只有方法类论文，就不要规划"实验数据集对比"；
   规划之前先想：这一段要写的内容，现有文献里真的有材料吗？
2. 每段必须有独立的 `query`：它是**检索用的中文查询**，不是段标题的复述。
   例如标题「参数高效微调的效果」对应 query 可以是「LoRA 在下游任务上的性能与全量微调对比」。
3. 禁止规划「参考文献 / References / 致谢」这类段——参考文献由系统从库内记录生成，
   不经过你，也不允许你写。
4. 段落顺序按论文惯例：背景 → 现状/方法 → 证据与对比 → 局限/结论。第一段可以是引言，
   但引言也必须落在证据上（写"这个方向为什么值得研究"要有文献依据）。

只输出 JSON。"""


def section_plan_prompt(topic: str, catalog_text: str, n_sections: int) -> str:
    return (
        f"写作主题：{topic}\n\n"
        f"库内可用文献（只有这些，不许假设还有别的）：\n{catalog_text}\n\n"
        f"请规划 {n_sections} 段左右的结构，输出严格 JSON：\n"
        '{"sections":[{"id":"S1","title":"","intent":"这一段要回答什么","query":"检索用中文查询"}]}\n'
        "要求：\n"
        "- `title` 用中文短标题，不要带序号（序号由系统渲染）；\n"
        "- `intent` 一句话说明本段的论证目标；\n"
        "- `query` 必须能在上面的文献里检到东西，不要写文献里没有的概念；\n"
        "- 段落之间要有推进关系（背景 → 方法 → 证据 → 局限），不要三段说同一件事。"
    )


# ------------------------------------------------------------- 2. 逐段写作

SECTION_WRITE_SYSTEM = """你是学术写作助手。给定**本段**的意图和证据清单，逐句写出这一段。

输出是**句子数组**，每句必须标类型：

- `claim`：陈述事实、结论、比较、数字的句子。**必须**以 `[引#N]` 结尾，N 只能取本段证据编号。
- `bridge`：纯粹的过渡与逻辑连接（"在此基础上""由此可见""下面分别讨论"）。
  这类句子**不需要也不能**带引用。
  **不许**把带判断的句子伪装成 bridge 来躲引用——凡是含具体数字、指标、比较
  （更优/更高/低于/提升/降低/超过/不如）、或对方法效果的断言，一律算 `claim`。
  服务端会复核：伪装成 bridge 的 claim 会被改判，改判后没有引用的**整句删除**。

写作规则（违反即该句作废）：
1. 只使用本段证据里的信息。禁止引入证据外的知识，禁止补充"常识"。
1.1 **证据必须真的在讲这件事**。如果本段证据的研究对象和本节标题不是同一个东西
   （比如本节问量子纠错、证据却在讲低秩适配），**不许硬凑**——
   文字上能拼成句子不代表结论成立。这种情况直接输出 `insufficient` 并说明缺什么。
2. **数字照抄**：正文里出现的百分比、小数、倍数必须在所引证据的原句里逐字出现。
   服务端会做数值落地校验，编造的数字会导致整句被删。
3. 一段 2~4 句。宁短勿长：写不出带引用的话，就少写一句。
4. 不要写标题、不要写"综上所述"、不要写参考文献、不要写 arXiv 号或作者-年份。
5. 专业术语保留英文原词（Transformer、LoRA、ViT）。
5.1 段落之间要有承接：本段若不是开头，第一句可以用 `bridge` 承接上文
   （"在此基础上""与之相对""除了上述收益之外"），再进入本段的结论。
   句与句之间的转折/递进也优先用 bridge 句连接，不要把两段逻辑硬塞进一个长 claim 句——
   长句里一旦混进两个证据，引用归属就说不清了。
6. 如果本段证据确实不足以支撑任何一句结论，**不要硬写**：
   输出 `{"sentences":[],"insufficient":true,"gap":"缺什么证据"}`，
   系统会在正文里放一个明确的占位块。空着比编造好。

只输出 JSON。"""


def section_write_prompt(
    topic: str,
    section_title: str,
    section_intent: str,
    evidence_text: str,
    prev_context: str,
    *,
    require_bridge: bool = False,
) -> str:
    """
    `require_bridge`：本段不是开头时把"第一项必须是 bridge"写成硬要求。

    为什么要把这句单拎出来：实测里模型会把承接逻辑**嵌进 claim 句**（"在此基础上，
    LoRA 将……[引#3]"），于是全篇一个 bridge 都没有——读者看到的每一句都带引用，
    反而分不清哪些是作者的话、哪些是文献的话。过渡句必须显式存在，
    这样"哪句不需要引用"才有答案。
    """
    bridge_rule = (
        "- **第一项必须是 `type=bridge` 的承接句**（承接上文，不带任何引用），"
        "然后再写 claim 句；\n"
        if require_bridge
        else ""
    )
    return (
        f"写作主题：{topic}\n\n"
        f"本段标题：{section_title}\n"
        f"本段意图：{section_intent or '（未给出）'}\n\n"
        f"## 本段证据（编号只能取这里的）\n\n{evidence_text}\n\n"
        f"## 上文最后一句（用于衔接，不要重复它的内容）\n\n{prev_context or '（本段是开头）'}\n\n"
        "请写出本段，输出严格 JSON：\n"
        '{"sentences":[{"type":"claim","text":"句子正文。[引#3]"},'
        '{"type":"bridge","text":"由此可见，"}]}\n'
        "要求：\n"
        "- 句子按正文顺序排列；`text` 里**不要**带序号、不要带 `- ` 列表符；\n"
        "- `claim` 句的 `[引#N]` 写在句号之后（与示例一致），一句可以带多个编号；\n"
        "- 编号必须是本段证据里出现过的整数，不要用别的段的编号；\n"
        f"{bridge_rule}"
        "- 段内如果出现转折/递进（但是、相比之下、因此），优先单独成一句 `bridge`；\n"
        "- 不要输出 `cite`/`refs` 之类的字段，引用只写在 `text` 里。"
    )


# ------------------------------------------------------------- 版本与配置

def prompts_fingerprint() -> str:
    blob = json.dumps(
        [WRITING_PROMPT_VERSION, SECTION_PLAN_SYSTEM, SECTION_WRITE_SYSTEM],
        ensure_ascii=False,
    )
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:10]


THRESHOLD_KEYS = (
    "writing_sections",
    "writing_evidence_per_section",
    "writing_per_paper",
    "writing_max_sentences",
    "writing_evidence_min_sim",
    "writing_strict_numbers",
)


def thresholds() -> dict[str, Any]:
    return {k: getattr(settings, k) for k in THRESHOLD_KEYS}


def config_block() -> dict[str, Any]:
    payload = json.dumps(
        {"th": thresholds(), "v": settings.writing_version}, ensure_ascii=False, sort_keys=True
    )
    return {
        "version": settings.writing_version,
        "prompt_version": WRITING_PROMPT_VERSION,
        "fingerprint": hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10],
        "thresholds": thresholds(),
    }
