# 流式对话事件摘要

- 问题：β2 的取值是多少？
- trace_id：`tr_7d4a9cb8e66b49d5`
- 首字节：32.5 ms，总时长：2288.4 ms
- 事件总数：17（stage 8 / token 6 / claim 1）
- 首 token：401.5 ms

## 阶段

| 阶段 | 相位 | 耗时 ms | 关键信息 |
| --- | --- | --- | --- |
| 理解问题 | start | None |  |
| 理解问题 | done | 562.3 | type_label=点查, translated=True |
| 检索 | start | None |  |
| 检索 | done | 261.1 | hits=21, injected=1 |
| 精排 | start | None |  |
| 精排 | done | 642.0 | applied=True, degraded=False |
| 生成 | start | None |  |
| 生成 | done | 792.5 | chars=14, stripped_markers=1 |

## 检索命中

- #1 `upload_bdfaa68d8984f0dc` p.7 · paragraph · score=0.8228 · anchors=Y
- #2 `2010.11929` p.5 · paragraph · score=0.5534 · anchors=N
- #3 `upload_bdfaa68d8984f0dc` p.5 · paragraph · score=0.3299 · anchors=Y
- #4 `2010.11929` p.9 · figure · score=0.3086 · anchors=N
- #5 `2010.11929` p.9 · figure · score=0.2963 · anchors=N
- #6 `2010.11929` p.9 · figure · score=0.2844 · anchors=N

## 主张与证据

| claim | 状态 | 正文 | 证据 |
| --- | --- | --- | --- |
| c1 | supported | β2 的取值是 0.98 。 | #1 p.7 s27 (supported, 0.9) |

支持率：1/1

归档核对：回放 17 条 / 实时 17 条 → 一致
