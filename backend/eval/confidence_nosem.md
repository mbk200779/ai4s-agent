# 07 · 分级置信：30 题分布统计

- 数据集：`v1.0.0`（30 题）
- 运行说明：关闭语义核验（CONFIDENCE_SEMANTIC_ENABLED=false），其余判据与主表一致
- 题面：`backend/eval/confidence_queries.json`
- 统计口径：`/api/v1/qa/ask` 返回的 `confidence.level`，按题计（一题一档）

## 总体分布

| 档位 | 题数 | 占比 | 含义 |
|---|---|---|---|
| verified | 8 | 27% | 已验证：原文有直接证据，可据此判定 |
| needs_check | 11 | 37% | 需核对：有相关证据，但需人工核对 |
| unsupported | 9 | 30% | 证据不足：没有找到能支撑的原文 |
| not_applicable | 2 | 7% | 不适用：闲聊/直答，不涉及文献证据 |
| **合计** | **30** | 100% | 失败 0 题 |

## 按题型

| 类别 | 题数 | verified | needs_check | unsupported | 不适用 |
|---|---|---|---|---|---|
| chitchat | 2 | 0 | 0 | 0 | 2 |
| compare | 5 | 0 | 5 | 0 | 0 |
| infer | 5 | 0 | 4 | 1 | 0 |
| out_of_scope | 8 | 0 | 1 | 7 | 0 |
| point | 6 | 5 | 1 | 0 | 0 |
| table | 4 | 3 | 0 | 1 | 0 |

## 「需核对」缺什么（按句子计）

| 缺项 | 句数 | 说明 |
|---|---|---|
| `no_citation` | 20 | 这句话没有引用标注 |
| `weak_match` | 9 | 证据句与原话重合度低于验证线 |
| `no_match` | 6 | 引用的片段里找不到能支撑这句话的句子 |
| `cross_section` | 3 | 证据由多处片段拼接而成，属于推断而非直引 |
| `single_paper` | 2 | 仅单篇论文支持，跨文献结论需再核一篇 |
| `entity_mismatch` | 1 | 结论中的专有名词未在引用片段中出现 |

## 口径校验

| 约束 | 结果 |
|---|---|
| 完整拒答只在「零片段」时出现 | 通过 |
| 需核对/证据不足必带缺项或理由（不得静默降级） | 通过 |
| 每条 claim 都有 level（无默认兜底） | 通过 |

## 逐题明细

| ID | 类别 | 档位 | 分 | 片段 | 已验证/需核对/不足 | 主要缺项 | 题目 |
|---|---|---|---|---|---|---|---|
| p1 | point | verified | 0.9 | 6 | 1/0/0 | - | Transformer 的 dropout 比例是多少？ |
| p2 | point | verified | 0.65 | 6 | 1/0/0 | - | Transformer 的多头注意力用了几个头？ |
| p3 | point | needs_check | 0.525 | 6 | 0/1/0 | weak_match | BERT-Base 有多少层 transformer block？ |
| p4 | point | verified | 0.9 | 6 | 1/0/0 | - | LoRA 在 GPT-3 上使用的秩 r 是多少？ |
| p5 | point | verified | 0.775 | 6 | 1/0/0 | - | ViT-Base 的 patch 大小是多少？ |
| p6 | point | verified | 0.7333 | 6 | 1/0/0 | - | ViT 的隐藏层维度是多少？ |
| t1 | table | verified | 0.9 | 6 | 1/0/0 | - | Transformer 在 WMT 2014 英德翻译任务上的 BLEU 分数是多少？ |
| t2 | table | verified | 0.6571 | 6 | 1/0/0 | - | ViT-H/14 在 ImageNet 上的 top-1 准确率是多少？ |
| t3 | table | verified | 0.5971 | 6 | 1/0/0 | - | LoRA 在 GPT-3 上把可训练参数减少了多少倍？ |
| t4 | table | unsupported | 0.0 | 6 | 0/0/4 | no_citation、no_match | BERT-Large 的参数量是多少？ |
| c1 | compare | needs_check | 0.1222 | 6 | 1/0/5 | no_citation、no_match | ViT 和 BERT 哪个模型参数量更大？ |
| c2 | compare | needs_check | 0.3 | 6 | 0/1/2 | no_citation、single_paper | LoRA 微调和全量微调在显存占用上有什么区别？ |
| c3 | compare | needs_check | 0.25 | 6 | 0/3/1 | no_match、weak_match | Transformer 与 BERT 的训练目标有什么不同？ |
| c4 | compare | needs_check | 0.7 | 6 | 1/1/0 | weak_match | ViT 和 BERT 分别用什么数据做预训练？ |
| c5 | compare | needs_check | 0.4857 | 6 | 0/1/0 | weak_match、single_paper | 这几篇论文里，哪个报告的 ImageNet 准确率最高？ |
| i1 | infer | needs_check | 0.5 | 6 | 0/1/0 | weak_match、cross_section | 为什么 Transformer 需要位置编码？ |
| i2 | infer | needs_check | 0.4143 | 6 | 1/0/1 | no_match | LoRA 为什么能降低显存占用？ |
| i3 | infer | needs_check | 0.2583 | 6 | 1/0/2 | no_citation、no_match | ViT 为什么需要大规模数据预训练？ |
| i4 | infer | needs_check | 0.425 | 6 | 0/3/1 | no_citation、weak_match | BERT 的 next sentence prediction 任务有什么用？ |
| i5 | infer | unsupported | 0.0 | 6 | 0/0/1 | no_match | 多头注意力相比单头注意力的优势是什么？ |
| o1 | out_of_scope | unsupported | 0.0 | 6 | 0/0/1 | no_citation | 这篇论文的作者获得过图灵奖吗？ |
| o2 | out_of_scope | needs_check | 0.1857 | 6 | 1/0/2 | no_citation | GPT-5 的参数量是多少？ |
| o3 | out_of_scope | unsupported | 0.0 | 6 | 0/0/1 | no_citation | 该方法申请了哪个专利？ |
| o4 | out_of_scope | unsupported | 0.0 | 6 | 0/0/1 | no_citation | Transformer 训练用了多少块 TPU？ |
| o5 | out_of_scope | unsupported | 0.0 | 6 | 0/0/1 | no_citation | 这几篇论文各自的引用次数是多少？ |
| o6 | out_of_scope | unsupported | 0.0 | 6 | 0/0/1 | no_citation | 这本书由哪家出版社出版？ |
| o7 | out_of_scope | unsupported | 0.0 | 6 | 0/0/1 | no_citation | 作者在 2025 年的后续工作是什么？ |
| o8 | out_of_scope | unsupported | 0.0 | 6 | 0/0/1 | no_citation | 这个模型的商业授权价格是多少？ |
| h1 | chitchat | not_applicable | 0.0 | 0 | 0/0/0 | - | 你好，你能做什么？ |
| h2 | chitchat | not_applicable | 0.0 | 0 | 0/0/0 | - | 谢谢！ |

## 延迟

- 平均 4222ms · 中位 3876ms · P90 7131ms · 最大 10192ms
- 含跨语言语义核验（每答 1~6 对，一次批量 embed，约 30~90ms）
