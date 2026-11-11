# 流式对话事件摘要

- 问题：RAG 如何缓解大语言模型的幻觉问题？
- trace_id：`tr_70c98457b0bf413b`
- 首字节：26.4 ms，总时长：2139.3 ms
- 事件总数：14（stage 8 / token 3 / claim 1）
- 首 token：601.8 ms

## 阶段

| 阶段 | 相位 | 耗时 ms | 关键信息 |
| --- | --- | --- | --- |
| 理解问题 | start | None |  |
| 理解问题 | done | 940.3 | type_label=通用兜底, translated=True |
| 检索 | start | None |  |
| 检索 | done | 486.1 | hits=20, injected=0 |
| 精排 | start | None |  |
| 精排 | done | 0.3 | applied=True, degraded=False |
| 生成 | start | None |  |
| 生成 | done | 707.5 | chars=5, stripped_markers=0 |

## 检索命中

- #1 `2106.09685` p.1 · paragraph · score=0.8914 · anchors=N
- #2 `2010.11929` p.2 · paragraph · score=0.8726 · anchors=N
- #3 `upload_bdfaa68d8984f0dc` p.0 · reference · score=0.8286 · anchors=N
- #4 `1810.04805` p.2 · paragraph · score=0.8242 · anchors=N
- #5 `2106.09685` p.3 · paragraph · score=0.8129 · anchors=N
- #6 `upload_bdfaa68d8984f0dc` p.2 · paragraph · score=0.8111 · anchors=Y

## 主张与证据

| claim | 状态 | 正文 | 证据 |
| --- | --- | --- | --- |
| c1 | unsupported | 无法回答。 | （无） |

支持率：0/1

归档核对：回放 14 条 / 实时 14 条 → 一致
