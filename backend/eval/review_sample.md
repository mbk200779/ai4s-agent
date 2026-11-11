# Transformer 架构的能力边界与适用条件

> 本综述基于 3 篇文献的 17 条结论自动生成，归并为 15 个观点簇（prompt review-v1.1.0 · 配置 rev-v1.0.0/4d2d1a0643）。
> 每段只陈述一个观点簇；已确认的分歧单独标注、不合并叙述。共 2 处冲突、15 个观点仅单篇支持。

## O1｜Transformer 完全依赖自注意力机制来处理输入和输出之间的全局依赖关系

> 立场：**支持** ｜ 证据：1 篇 / 3 条，**仅单篇支持** ｜ 轴：Transformer：方法设计

Transformer 完全依赖自注意力机制来处理输入和输出之间的全局依赖关系，摒弃了循环和卷积。[引#6][引#15]多头注意力机制允许模型在不同位置从不同的表示子空间中联合获取信息。[引#9]

## O2｜LoRA 的推断延迟低于全量微调和适配器方法

> 立场：**支持** ｜ 证据：1 篇 / 1 条，**仅单篇支持** ｜ 轴：LoRA 相对 全量微调/适配器方法：推断延迟

LoRA 在部署时不会增加额外的推断延迟。[引#2]

## O3｜对于 WikiSQL 和 MultiNLI 数据集，使用 LoRA 时，即使 rank 很小也能达到较好的性能

> 立场：**有条件成立** ｜ 证据：1 篇 / 1 条，**仅单篇支持** ｜ 轴：LoRA 相对 全量微调：下游任务性能
> 成立条件：rank 较小

对于 WikiSQL 和 MultiNLI 数据集，即使 rank 很小，LoRA 也能够达到较好的性能。[引#14]

## O4｜LoRA 在多个下游任务中表现与全量微调相当或更好

> 立场：**支持** ｜ 证据：1 篇 / 1 条，**仅单篇支持** ｜ 轴：LoRA 相对 全量微调：下游任务性能

LoRA 在多个下游任务中表现与全量微调相当或更好。[引#8]

## O5｜在 GPT-3 上，适应 Wq 和 Wv 可以获得最佳性能

> 立场：**支持** ｜ 证据：1 篇 / 1 条，**仅单篇支持** ｜ 轴：LoRA 相对 全量微调：下游任务性能

在 GPT-3 上，适应 Wq 和 Wv 可以获得最佳性能。[引#14]

## O6｜LoRA 可以减少训练参数的数量并提高训练吞吐量

> 立场：**支持** ｜ 证据：1 篇 / 1 条，**仅单篇支持** ｜ 轴：LoRA 相对 全量微调：训练参数数量

LoRA 可以减少训练参数的数量并提高训练吞吐量。[引#5]

## O7｜LoRA 在任务适应中的更新权重具有低秩结构

> 立场：**支持** ｜ 证据：1 篇 / 1 条，**仅单篇支持** ｜ 轴：LoRA：方法设计

LoRA 在任务适应中的更新权重具有低秩结构。[引#2]

## O8｜Transformer 允许更高的并行化程度

> 立场：**支持** ｜ 证据：1 篇 / 1 条，**仅单篇支持** ｜ 轴：Transformer 相对 RNN：训练并行度

Transformer 允许更高的并行化程度，并且可以在短时间内达到新的翻译质量水平。[引#15]

## O9｜Vision Transformers 在性能/计算成本权衡上优于 ResNets

> 立场：**支持** ｜ 证据：1 篇 / 1 条，**仅单篇支持** ｜ 轴：Vision Transformers 相对 ResNets：性能/计算成本

Vision Transformers 在性能/计算成本权衡上优于 ResNets。[引#7]

## O10｜Vision Transformers 在小计算预算下略逊于混合模型

> 立场：**反对**（主张该轴的反向结论） ｜ 证据：1 篇 / 1 条，**仅单篇支持** ｜ 轴：Vision Transformers 相对 混合模型：性能/计算成本
> 成立条件：小计算预算

在小计算预算下，混合模型略优于 Vision Transformers。[引#7]

## C2｜冲突：O9 ↔ O10（条件性分歧·已确认）

> **O9｜支持（1 篇）**：Vision Transformers 在性能/计算成本权衡上优于 ResNets
> **O10｜反对（1 篇）**：Vision Transformers 在小计算预算下略逊于混合模型
> 判据：A 和 B 的结论分别基于无特定条件和小计算预算条件，条件不同导致了性能/计算成本的权衡差异。
> 轴：Vision Transformers 相对 ResNets：性能/计算成本

Vision Transformers 在性能/计算成本权衡上优于 ResNets。[引#7]

在小计算预算下，混合模型略优于 Vision Transformers。[引#7]

## O11｜Vision Transformers 的位置嵌入学习到了 2D 图像拓扑结构

> 立场：**支持** ｜ 证据：1 篇 / 1 条，**仅单篇支持** ｜ 轴：Vision Transformers：方法设计

Vision Transformers 的位置嵌入学习到了 2D 图像拓扑结构。[引#7]

## O12｜ViT-L/16 在 JFT-300M 上预训练后的性能优于 BiT-L

> 立场：**有条件成立** ｜ 证据：1 篇 / 1 条，**仅单篇支持** ｜ 轴：ViT-L/16 相对 BiT-L：性能
> 成立条件：JFT-300M 预训练

ViT-L/16 在 JFT-300M 上预训练后的性能优于 BiT-L。[引#10]

## O13｜ViT 在小规模数据集上的表现不如 BiT ResNets

> 立场：**反对**（主张该轴的反向结论） ｜ 证据：1 篇 / 1 条，**仅单篇支持** ｜ 轴：ViT 相对 BiT ResNet：ImageNet 准确率
> 成立条件：小规模数据集

ViT 在小规模数据集上的表现不如 BiT ResNets。[引#1]

## O14｜ViT 在大规模数据集上的表现优于 BiT ResNets

> 立场：**支持** ｜ 证据：1 篇 / 1 条，**仅单篇支持** ｜ 轴：ViT 相对 BiT ResNet：ImageNet 准确率

ViT 在大规模数据集上的表现优于 BiT ResNets。[引#1]

## C1｜冲突：O13 ↔ O14（条件性分歧·已确认）

> **O13｜反对（1 篇）**：ViT 在小规模数据集上的表现不如 BiT ResNets
> **O14｜支持（1 篇）**：ViT 在大规模数据集上的表现优于 BiT ResNets
> 判据：A 和 B 的结论分别基于小规模数据集和大规模数据集，条件不同导致了表现差异。
> 轴：ViT 相对 BiT ResNet：ImageNet 准确率

ViT 在小规模数据集上的表现不如 BiT ResNets。[引#1]

ViT 在大规模数据集上的表现优于 BiT ResNets。[引#1]

## O15｜ViT 将图像分割成固定大小的块并线性嵌入这些块

> 立场：**支持** ｜ 证据：1 篇 / 1 条，**仅单篇支持** ｜ 轴：ViT：方法设计

ViT 将图像分割成固定大小的块并线性嵌入这些块。[引#13]

## 附：证据强度汇总（系统生成，非文献结论）

- O1「Transformer 完全依赖自注意力机制来处理输入和输出之间的全局依赖关系」—— 仅单篇支持（Attention is All You Need论文.pdf）
- O2「LoRA 的推断延迟低于全量微调和适配器方法」—— 仅单篇支持（LoRA: Low-Rank Adaptation of Large Language Models）
- O3「对于 WikiSQL 和 MultiNLI 数据集，使用 LoRA 时，即使 rank 很小也能达到较好的性能」—— 仅单篇支持（LoRA: Low-Rank Adaptation of Large Language Models）
- O4「LoRA 在多个下游任务中表现与全量微调相当或更好」—— 仅单篇支持（LoRA: Low-Rank Adaptation of Large Language Models）
- O5「在 GPT-3 上，适应 Wq 和 Wv 可以获得最佳性能」—— 仅单篇支持（LoRA: Low-Rank Adaptation of Large Language Models）
- O6「LoRA 可以减少训练参数的数量并提高训练吞吐量」—— 仅单篇支持（LoRA: Low-Rank Adaptation of Large Language Models）
- O7「LoRA 在任务适应中的更新权重具有低秩结构」—— 仅单篇支持（LoRA: Low-Rank Adaptation of Large Language Models）
- O8「Transformer 允许更高的并行化程度」—— 仅单篇支持（Attention is All You Need论文.pdf）
- O9「Vision Transformers 在性能/计算成本权衡上优于 ResNets」—— 仅单篇支持（An Image is Worth 16x16 Words: Transformers for Image Recogn）
- O10「Vision Transformers 在小计算预算下略逊于混合模型」—— 仅单篇支持（An Image is Worth 16x16 Words: Transformers for Image Recogn）
- O11「Vision Transformers 的位置嵌入学习到了 2D 图像拓扑结构」—— 仅单篇支持（An Image is Worth 16x16 Words: Transformers for Image Recogn）
- O12「ViT-L/16 在 JFT-300M 上预训练后的性能优于 BiT-L」—— 仅单篇支持（An Image is Worth 16x16 Words: Transformers for Image Recogn）
- O13「ViT 在小规模数据集上的表现不如 BiT ResNets」—— 仅单篇支持（An Image is Worth 16x16 Words: Transformers for Image Recogn）
- O14「ViT 在大规模数据集上的表现优于 BiT ResNets」—— 仅单篇支持（An Image is Worth 16x16 Words: Transformers for Image Recogn）
- O15「ViT 将图像分割成固定大小的块并线性嵌入这些块」—— 仅单篇支持（An Image is Worth 16x16 Words: Transformers for Image Recogn）

## 参考文献

1. An Image is Worth 16x16 Words: Transformers for Image Recognition at Scale (arXiv:2010.11929)
2. LoRA: Low-Rank Adaptation of Large Language Models (arXiv:2106.09685)
3. Attention is All You Need论文.pdf (arXiv:upload_bdfaa68d8984f0dc)
