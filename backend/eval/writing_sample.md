# 注意力架构的能力边界：从 Transformer 到 ViT

> 生成口径：`writing-v1.2.0` ｜ 配置 `wrt-v1.0.0`/50316b3fa9 ｜ 正文 11 句（结论句 8、连接句 3） ｜ 参考文献 3 条（全部来自库内） ｜ 无证据占位 0 段

## 1. 注意力机制的引入与重要性

注意力机制已成为现代深度学习架构中不可或缺的一部分，尤其是在序列建模和转换任务中，如语言建模和机器翻译。[引#3]
基于注意力机制的 Transformer 架构在多项任务中表现出色，并且可以显著提高计算效率和并行化能力。[引#3][引#4]

## 2. 从文本到图像：ViT的应用

在上文关于「注意力机制的引入与重要性」的讨论基础上，本节进一步讨论「从文本到图像：ViT的应用」。
ViT通过将图像分割成patches，并将这些patches的线性嵌入作为输入提供给Transformer，能够有效应用于图像识别任务。[引#1]
ViT在大规模数据预训练后，在多个中等或小规模图像识别基准测试（如ImageNet、CIFAR-100、VTAB等）上取得了与最先进的卷积网络相当甚至更好的结果，同时所需的计算资源显著减少。[引#1]

## 3. 实际效果对比

在上文关于「从文本到图像：ViT的应用」的讨论基础上，本节进一步讨论「实际效果对比」。
BERT在多种自然语言处理任务中取得了显著的性能提升，例如在GLUE评分上达到了80.5%，相比于之前的模型提升了7.7个百分点。[引#5]
ViT在图像分类任务中也表现出色，特别是在大规模数据预训练后，在多个基准测试上的表现优于传统卷积网络，并且所需的计算资源更少。[引#1]

## 4. 现有架构面临的挑战

尽管如此，在实际应用中，基于注意力机制的模型仍然面临一些挑战。
例如，Transformer 模型在处理长序列时存在计算效率问题，因为其复杂度随序列长度呈平方级增长。[引#3]
当在中等规模的数据集如 ImageNet 上进行训练时，这些模型的精度比同等大小的 ResNet 低几个百分点。[引#1]

## 参考文献

[1] Attention is All You Need论文.pdf. arXiv:upload_bdfaa68d8984f0dc.
[2] Alexey Dosovitskiy, Lucas Beyer, Alexander Kolesnikov et al. An Image is Worth 16x16 Words: Transformers for Image Recognition at Scale. arXiv:2010.11929.
[3] Jacob Devlin, Ming-Wei Chang, Kenton Lee et al. BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding. arXiv:1810.04805.

## 引文映射（导出保留）

| 引# | 文献 | 题名 | 页 | chunk_id |
|---|---|---|---|---|
| 引#1 | [2] | An Image is Worth 16x16 Words: Transformers for Image Recognition at Scale | p.1 | `807e635f-e396-f258-5734-17af0563ecd2` |
| 引#3 | [1] | Attention is All You Need论文.pdf | p.2 | `chk_12fa20f0918e445d` |
| 引#4 | [1] | Attention is All You Need论文.pdf | p.1 | `chk_830ed3cc15ca4932` |
| 引#5 | [3] | BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding | p.1 | `0205baa8-df7d-17a8-cda9-78b2110110c4` |

> 另有 2 段证据被检索到但未被正文引用（见导出 json 的 `ref_map`）。
