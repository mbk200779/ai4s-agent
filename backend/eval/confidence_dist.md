# 07 · 分级置信：30 题分布统计

- 数据集：`v1.0.0`（30 题）
- 运行说明：模型 dashscope/qwen-max（qwen-plus 免费额度耗尽，用同一套判据换模型跑）
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
| infer | 5 | 1 | 3 | 1 | 0 |
| out_of_scope | 8 | 0 | 1 | 7 | 0 |
| point | 6 | 4 | 2 | 0 | 0 |
| table | 4 | 3 | 0 | 1 | 0 |

## 「需核对」缺什么（按句子计）

| 缺项 | 句数 | 说明 |
|---|---|---|
| `no_citation` | 25 | 这句话没有引用标注 |
| `weak_match` | 6 | 证据句与原话重合度低于验证线 |
| `no_match` | 2 | 引用的片段里找不到能支撑这句话的句子 |
| `single_paper` | 2 | 仅单篇论文支持，跨文献结论需再核一篇 |
| `cross_section` | 2 | 证据由多处片段拼接而成，属于推断而非直引 |
| `no_experiment` | 1 | 结论含比较表述，但证据里没有对照实验或基线数值 |
| `no_table` | 1 | 结论中的数值不在证据原句中，缺表格原始数据 |

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
| p4 | point | needs_check | 0.3 | 6 | 1/0/2 | no_citation | LoRA 在 GPT-3 上使用的秩 r 是多少？ |
| p5 | point | verified | 0.775 | 6 | 1/0/0 | - | ViT-Base 的 patch 大小是多少？ |
| p6 | point | verified | 0.7333 | 6 | 1/0/0 | - | ViT 的隐藏层维度是多少？ |
| t1 | table | verified | 0.9 | 6 | 1/0/0 | - | Transformer 在 WMT 2014 英德翻译任务上的 BLEU 分数是多少？ |
| t2 | table | verified | 0.6571 | 6 | 1/0/0 | - | ViT-H/14 在 ImageNet 上的 top-1 准确率是多少？ |
| t3 | table | verified | 0.7167 | 6 | 1/0/0 | - | LoRA 在 GPT-3 上把可训练参数减少了多少倍？ |
| t4 | table | unsupported | 0.0 | 6 | 0/0/4 | no_citation、no_match | BERT-Large 的参数量是多少？ |
| c1 | compare | needs_check | 0.2571 | 6 | 2/0/5 | no_citation | ViT 和 BERT 哪个模型参数量更大？ |
| c2 | compare | needs_check | 0.3333 | 6 | 0/1/2 | no_citation、single_paper | LoRA 微调和全量微调在显存占用上有什么区别？ |
| c3 | compare | needs_check | 0.4457 | 6 | 1/2/0 | weak_match、no_experiment | Transformer 与 BERT 的训练目标有什么不同？ |
| c4 | compare | needs_check | 0.5091 | 6 | 1/1/1 | no_citation、weak_match | ViT 和 BERT 分别用什么数据做预训练？ |
| c5 | compare | needs_check | 0.375 | 6 | 0/1/0 | weak_match、no_table | 这几篇论文里，哪个报告的 ImageNet 准确率最高？ |
| i1 | infer | needs_check | 0.25 | 6 | 0/1/1 | no_citation、weak_match | 为什么 Transformer 需要位置编码？ |
| i2 | infer | needs_check | 0.6667 | 6 | 2/0/1 | no_citation | LoRA 为什么能降低显存占用？ |
| i3 | infer | needs_check | 0.45 | 6 | 1/0/1 | no_citation | ViT 为什么需要大规模数据预训练？ |
| i4 | infer | verified | 0.8266 | 6 | 1/0/0 | - | BERT 的 next sentence prediction 任务有什么用？ |
| i5 | infer | unsupported | 0.0 | 6 | 0/0/2 | no_citation、no_match | 多头注意力相比单头注意力的优势是什么？ |
| o1 | out_of_scope | unsupported | 0.0 | 6 | 0/0/1 | no_citation | 这篇论文的作者获得过图灵奖吗？ |
| o2 | out_of_scope | unsupported | 0.0 | 6 | 0/0/1 | no_citation | GPT-5 的参数量是多少？ |
| o3 | out_of_scope | unsupported | 0.0 | 6 | 0/0/1 | no_citation | 该方法申请了哪个专利？ |
| o4 | out_of_scope | needs_check | 0.4485 | 6 | 2/0/1 | no_citation | Transformer 训练用了多少块 TPU？ |
| o5 | out_of_scope | unsupported | 0.0 | 6 | 0/0/1 | no_citation | 这几篇论文各自的引用次数是多少？ |
| o6 | out_of_scope | unsupported | 0.0 | 6 | 0/0/1 | no_citation | 这本书由哪家出版社出版？ |
| o7 | out_of_scope | unsupported | 0.0 | 6 | 0/0/1 | no_citation | 作者在 2025 年的后续工作是什么？ |
| o8 | out_of_scope | unsupported | 0.0 | 6 | 0/0/1 | no_citation | 这个模型的商业授权价格是多少？ |
| h1 | chitchat | not_applicable | 0.0 | 0 | 0/0/0 | - | 你好，你能做什么？ |
| h2 | chitchat | not_applicable | 0.0 | 0 | 0/0/0 | - | 谢谢！ |

## 延迟

- 平均 4559ms · 中位 3747ms · P90 7056ms · 最大 12898ms
- 含跨语言语义核验（每答 1~6 对，一次批量 embed，约 30~90ms）
