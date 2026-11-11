# 检索分流评测：分流前 vs 分流后

- 数据集：routing-v1（18 条）
- top_k = 8；基线 = `RetrievalService.search()`（分流前唯一路径），分流侧 = `RAGRetriever.route()`
- 指标只看召回：hit@k 表示 gold（论文+页码）是否进入 top_k，与答案文本无关

## 汇总

| 侧 | hit@k | MRR | 平均耗时 | 论文覆盖(对比题) | 类型准确率 | 分类耗时均值/峰值 |
|---|---|---|---|---|---|---|
| 分流前 | 88.9% (16/18) | 0.648 | 206.9 ms | 0.8333 | — | — / — ms |
| 分流后 | 100.0% (18/18) | 0.931 | 137.4 ms | 0.9286 | 1.0 | 12.1 / 95.1 ms |
| **差值** | **+11.1%** | **+0.282** | -69.5 ms | | | |

## 逐条对比

| ID | 问题 | 期望类型 | 分流后判定 | 分流前 hit | 分流后 hit | MRR 前→后 | 召回论文数 |
|---|---|---|---|---|---|---|---|
| p-beta2 | Adam 优化器的 β2 设为多少 | point | point (rules) | ✗ | ✓ | 0.00 → 1.00 | 3 |
| p-warmup | 学习率 warmup 步数是多少 | point | point (rules) | ✓ | ✓ | 0.17 → 1.00 | 3 |
| p-gpu | 训练用了多少块 GPU，什么型号 | point | point (rules) | ✓ | ✓ | 0.25 → 0.50 | 4 |
| p-vit-batch | ViT 训练时的 batch size 是多少 | point | point (rules) | ✓ | ✓ | 0.50 → 1.00 | 2 |
| p-bert-hidden | BERT base 模型的 hidden size 是… | point | point (rules) | ✓ | ✓ | 1.00 → 1.00 | 2 |
| p-att-bleu | Transformer 在 WMT2014 英德任务上… | point | point (rules) | ✓ | ✓ | 1.00 → 1.00 | 3 |
| n-warmup-quote | 原文里 "warmup_steps = 4000" 出… | nav | nav (rules) | ✓ | ✓ | 0.50 → 1.00 | 4 |
| n-label-smooth | 哪篇论文提到 "label smoothing" | nav | nav (rules) | ✓ | ✓ | 0.50 → 1.00 | 3 |
| n-low-rank | 原文里 "low-rank" 这个说法出现在哪一段 | nav | nav (rules) | ✓ | ✓ | 0.50 → 1.00 | 1 |
| c-bert-transformer | BERT 与 Transformer Encoder … | compare | compare (rules) | ✓ | ✓ | 1.00 → 1.00 | 4 |
| c-beta-diff | Transformer 与 ViT 里 β2 的取值有… | compare | compare (rules+prototype) | ✓ | ✓ | 1.00 → 1.00 | 4 |
| c-lora-bert | LoRA 和 BERT 在参数规模上有什么差异 | compare | compare (rules) | ✓ | ✓ | 1.00 → 1.00 | 2 |
| c-three-methods | 这三篇的方法有何差异 | compare | compare (rules) | ✓ | ✓ | 1.00 → 1.00 | 4 |
| s-peft | 参数高效微调有哪些主要方法 | survey | survey (rules) | ✓ | ✓ | 1.00 → 1.00 | 3 |
| s-attention | 注意力机制都有哪些主要变体 | survey | survey (rules) | ✓ | ✓ | 1.00 → 1.00 | 5 |
| ch-hello | 你好 | chitchat | chitchat (rules) | ✗ | ✓ | 0.00 → 1.00 | 0 |
| g-selfattn | self-attention 是怎么计算的 | generic | generic (fallback) | ✓ | ✓ | 1.00 → 1.00 | 3 |
| g-bert-pretrain | BERT 的预训练任务是什么 | generic | generic (fallback) | ✓ | ✓ | 0.25 → 0.25 | 1 |
