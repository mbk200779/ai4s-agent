"""
全局配置模块
关键设计决策：所有外部连接参数均通过环境变量注入，本地与云端共用同一份代码，
避免在代码里写死任何环境相关常量。
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 复现性控制
    temperature: float = 0.0
    prompt_version: str = "v1.0.0"

    # Qdrant 向量库
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "ai4s_papers"
    qdrant_paper_meta_collection: str = "ai4s_paper_meta"

    # 关系型元数据（课题/论文/版本/chunk/引用）。向量仍在 Qdrant，
    # 两边通过 chunks.qdrant_point_id 关联。
    db_path: str = "./data/ai4s.db"
    # 长文档解析的工作目录：流式下载的 PDF 与断点状态都放这里。
    # 必须落在持久卷上，容器重启后断点才成立。
    ingest_work_dir: str = "./data/ingest"
    # 单文档的解析上限（页数/内存等）见 services/ingest/limits.py，
    # 那些是代码常量而不是配置项——上限一旦可配，就会有人把它调成 0 来"跑通一次"。
    # 旧数据迁移与未指定课题的上传都落到这个课题，保证「论文必须归属课题」不被绕过
    default_project_name: str = "默认课题"
    pipeline_version: str = "v1.0.0"

    # ONNX 推理线程数。默认（不指定）会让 onnxruntime 按核数开线程，
    # 单条短文本反而被线程调度拖慢：实测同一句 16 核默认 197~350ms、8 线程 40ms；
    # 20 条长文本批量 8.8s vs 4.4s。所以这里显式定 8，并用环境变量留出调整余地。
    embed_threads: int = 8

    # 精排（cross-encoder）。开关放在配置里而不是代码常量里，
    # 是为了"一键关掉做 A/B"：改环境变量重启即可，不需要改代码分支。
    # 单次请求也能用 `rerank=false` 覆盖，评测脚本靠它跑同一批问题的两种配置。
    rerank_enabled: bool = True
    # 模型选择是延迟与语言的权衡（见 docs/2.9 的实测表）：
    #   Xenova/ms-marco-MiniLM-L-6-v2  20 对约 320~410ms，英文
    #   jinaai/jina-reranker-v1-turbo-en  20 对约 500~670ms，英文
    #   jinaai/jina-reranker-v2-base-multilingual  20 对约 2.5s，多语言
    # 默认取最快的那一个：精排是链路里唯一"可省"的一段，不能让它成为延迟主项。
    rerank_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    # 精排预算。超时即降级为融合原序（不报错），并进入 30s 熔断。
    # 1500ms 的依据：20 对 (query, 400 字符) 稳态实测 360~500ms，均值 ~630ms（不含规则召回），
    # 但分布右尾有 1200ms 的样本（数字密集型长片段）——1200 这条线正好穿过右尾，
    # 评测里表现为偶发 1/18 降级。给右尾留 25% 余量取 1500，代价是"最坏情况多等 0.3s"，
    # 对总时长 1.5~3s 的问答链路可接受；再大就会让单条问答被精排拖过 2s。
    # 模型加载与热身不计入这个预算（只发生一次），见 Reranker._score_pairs。
    rerank_timeout_ms: int = 1500
    # 精排分在最终排序里的权重。1.0 = 精排完全接管，0 = 等于不开精排。
    # 纯精排在本语料上把 Recall@5 从 78.8% 压到 54.5%（见 docs/2.9 的 α 扫描），
    # 因为通用 cross-encoder 不认识"β2 = 0.98"这种字面证据的价值。
    # 0.2 / 0.3 的 Recall 相同，但 0.2 的 MRR 更好（routing 0.875 vs 0.847），故取 0.2。
    rerank_alpha: float = 0.2
    # 规则命中的证据加成（加在最终分上，与 α 无关）。
    # 字面命中比任何语义分都硬，不该被语义模型压下去。
    rerank_rule_bonus: float = 0.35
    # 数值/公式规则召回（正则扫全语料，命中表格单元格优先）。
    # 和精排分开开关：这两个改动的收益来源不同，A/B 时要能各关各的。
    numeric_recall_enabled: bool = True

    # 句子级锚点：默认开。关掉时解析仍然照常跑，只是 chunks.anchors 为空，
    # 前端会退化成"页级定位 + 未建立精细锚点"提示——这正是历史文档的表现，
    # 所以这个开关同时是"复现旧行为"的开关。
    anchor_enabled: bool = True
    # 扫描件 OCR。镜像内已装 tesseract（含 eng / chi_sim），默认开：
    # 官方测试用例里有纯图像 PDF（1706.03762v1_img.pdf），关掉就解析出空文本。
    # 只在「本页没有文本层」时才调用，正常论文不受影响。
    # 打开后该页锚点自动标 precision='ocr'，前端画虚框并注明「OCR 定位」。
    ingest_ocr_enabled: bool = True
    ingest_ocr_lang: str = "eng"
    ingest_ocr_dpi: int = 200

    # 分级置信（07）。三档阈值集中在这里，改配置即可改分档，不需要动代码：
    #   verified_score      ：证据句与原话的重合度到这条线，且没有"缺什么"的硬伤 → 已验证
    #   needs_check_score   ：低于这条线就判无证据（与 claim_min_evidence 同源，
    #                         但单独留一个键，避免"调证据门槛"顺手改掉了分档口径）
    # 0.55 的依据：本语料上直引句（claim 几乎逐字来自原文）落在 0.6~0.95，
    # 而"跨句改写/只有关键词对上"的落在 0.2~0.45，中间 0.45~0.6 基本没有样本，
    # 取 0.55 是把边界放在两簇之间的空档里，不是取中位数。
    confidence_verified_score: float = 0.55
    confidence_needs_check_score: float = 0.15
    # 跨文献结论（对比/综述）要求至少几篇独立论文才算"已验证"。
    confidence_cross_paper_min_sources: int = 2
    # 跨语言语义核验（中文结论 ↔ 英文原句）。
    # 关掉后只用字面分：中文表述的结论会一律落到 needs_check —— 这是"保守但不装懂"，
    # 也是这个开关的用途（A/B 看出语义核验到底值了多少条 verified）。
    confidence_semantic_enabled: bool = True
    # 余弦 → 支持度 的线性映射区间。实测无关对 ≤0.18、同义对 ≥0.51，取 [0.35, 0.85]。
    confidence_semantic_floor: float = 0.35
    confidence_semantic_ceiling: float = 0.85
    # 阈值版本号。每次调整上面的数值都应该同时 bump 它，
    # 响应体与 provenance 里都会带回 version + fingerprint，复盘时能确认"当时用的是哪套线"。
    confidence_version: str = "conf-v1.0.0"
    # 低分是否直接整体拒答。默认 False：07 起"检索分低"不再等于"不作答"，
    # 而是把结论标成 needs_check 并要求人工核对——只有"一条都没检索到"才完整拒答。
    # 置 True 可复现 06 之前的旧口径（评测对比用）。
    reject_on_low_score: bool = False

    # ---- 12 不确定性状态（empty / conflict / unreadable）----
    # 空检索的严格模式：检索偏弱（融合原始分低于策略阈值）**且**问句检索词在库内 0 命中时，
    # 判为空检索并跳过生成。默认 True。
    # 为什么需要它：dense 检索永远会返回最近邻，"零片段"在真实系统里几乎不发生，
    # 于是"库里没有"这件事从来不会以空检索的形态出现——它被写成了似是而非的答案。
    # 关掉后只保留"一条都没召回"这一种（旧口径），用于 A/B。
    unc_empty_strict: bool = True
    # 内容词最短长度与最多取几个。太短的词（AI/ML）几乎处处命中，判据会被它放水。
    unc_min_term_len: int = 3
    unc_max_terms: int = 12
    # 一次回答最多报几处冲突 / 几处未识别。多了会把正文挤没，读者反而看不到重点。
    unc_conflict_max: int = 3
    unc_unreadable_max: int = 4
    unc_version: str = "unc-v1.0.0"

    # ---- 13 索引 pipeline 版本化 ----
    # 重建时一批编码多少条片段。它是**唯一**影响重建内存的旋钮：
    # 一次编码 N 条 × 1500 字符，峰值内存大致与 N 成正比，
    # 而耗时在 N 过小时被 ONNX 的固定开销拖住（实测见 docs/2.17 的成本表）。
    index_batch_size: int = 64
    # 保留多少个历史版本。旧版本不删是"可回滚"的前提，但也不能无限留：
    # 每个版本都是一份完整的 chunk 向量（本语料 279 段约 12MB，五十万段就是 20GB）。
    # 超出的版本只在 API 显式删除时清掉，这里仅作为界面提示的阈值。
    index_keep_versions: int = 3

    # 结构化生成协议（SSE）。stream_enabled 关掉后 /qa/stream 直接 503，
    # 前端自动退回一次性 /qa/ask —— 出问题时要能一键回到旧链路，而不是靠回滚镜像。
    stream_enabled: bool = True
    # claim 的证据分数线。低于它就标 unsupported 但**保留**在结果里：
    # 静默删除会让"这句话没证据"这个事实消失，比留着更危险。
    claim_min_evidence: float = 0.15
    # 是否把每次对话的完整事件日志写到 MinIO（qa/stream/<日期>/<trace_id>.jsonl）。
    # 答辩回放与线上排障都靠它；关掉只影响归档，不影响流本身。
    stream_log_enabled: bool = True
    # 心跳间隔（秒）。SSE 在反代/隧道上静默超过 30s 会被切断，
    # 而"检索 + 精排"这段可能十几秒没有任何事件，必须靠注释行保活。
    stream_heartbeat_s: float = 8.0

    # ---- 综述生成：观点聚类与冲突标注 ----
    # 注意：综述的 prompt 版本**不在这里**。它跟着 prompt 文本走，定义在
    # `services/review/prompts.py: REVIEW_PROMPT_VERSION`——放两份就会漂
    # （改了 prompt 忘改配置，响应体报的版本号就是假的）。
    # 证据池规模。综述要的是"覆盖"，不是 top-k 相关：
    # 单篇最多取 review_pool_per_paper 段（防止一篇论文刷满证据池），总量封顶 review_pool_max_chunks。
    review_pool_per_paper: int = 5
    review_pool_max_chunks: int = 20
    # 主题扩展出的子问题数（方法/效果/局限…）。0 表示不做扩展，只按主题检索。
    review_subqueries: int = 3
    # 单次抽 claim 的片段数上限。片段太多会把上下文挤爆、且后面每条 claim 都要带引用，宁少勿滥。
    review_claim_batch: int = 12
    # 轴归并的**兜底**阈值：只在 LLM 归并不可用时生效（`cluster._lexical_canon`）。
    # 主路径是 LLM 归并，因为"两个轴是不是同一个维度"不是相似度问题（见 cluster.py 注释）。
    review_axis_merge_sim: float = 0.78
    # 同一个 (轴, 极性) 桶内再按结论相似度切分。轴是模型打的标签、会打错，
    # 这条线是独立于轴的第二道判断：结论不像就不许并簇。
    review_statement_split_sim: float = 0.5
    # 冲突二次召回的轴相似度门槛。比归并阈值松：漏召回 = 把分歧写没了，误召回只是多一次复核。
    review_conflict_axis_sim: float = 0.72
    # 冲突候选对的"结论相似度"门槛。一对互斥结论必须说的是同一件事、只是方向相反；
    # 连结论都不像的两条，属于"两件事"而不是"一对矛盾"，不该占用复核名额。
    review_conflict_statement_sim: float = 0.6
    # 交给 LLM 复核的冲突对上限。规则召回可能冒出十几对，全丢给 LLM 既慢又容易把
    # "条件不同"误判成"互相矛盾"，按轴相似度排序取前 N 对。
    review_conflict_llm_pairs: int = 6
    # 综述配置版本（阈值/池大小改动时 bump，与 prompt_version 分开记）。
    review_version: str = "rev-v1.0.0"

    # ---------------- 09 写作辅助：句级溯源 ----------------
    # 规划几段。段数多了每段只能分到 1~2 条证据，写出来全是"仅单篇"，反而更空；
    # 4 段是本语料（5 篇论文）下"每段还能拿到 3 条以上证据"的上限经验值。
    writing_sections: int = 4
    # 每段的证据条数。段内 6 条已经能覆盖 3 篇论文的两三个角度。
    writing_evidence_per_section: int = 6
    # 每段里同一篇论文最多几条。写作比综述更容易被一篇占满：段落更短，一篇就能填满。
    writing_per_paper: int = 2
    # 每段句子上限。2~4 句是任务要求，留 5 是给"过渡句 + 4 句结论"的情况。
    writing_max_sentences: int = 5
    # 证据相关性下限（跨语言余弦，见 services/rag/semantic.py）。
    # 为什么需要它：检索永远返回 top-k，哪怕库里根本没有这个话题，也会把表格残渣、
    # 别的论文的段落端上来。实测「量子纠错码的表面码容错阈值」这个主题，
    # 检索回来的仍是 LoRA 的段落——模型于是写出"低秩适应在量子计算中的应用"这种
    # 每句都有引用、但整段是胡话的东西。这条线是"无证据就留空"的第一道闸。
    # 0.30 的来历（实测见 docs/2.13 §7）：同主题正文 0.34~0.60，跑题正文 0.19~0.22；
    # 0.30~0.34 之间的是页眉/版权行（"Published as a conference paper…"），
    # 那类片段另外由 evidence._BOILERPLATE_RE 按版式剔掉，不靠这条线兜。
    writing_evidence_min_sim: float = 0.30
    # 数字落地校验开关。关掉它只该在对比实验里做——关掉就意味允许编数字。
    writing_strict_numbers: bool = True
    # 写作配置版本（阈值改动时 bump，与 prompt 版本分开记）。
    writing_version: str = "wrt-v1.0.0"

    # Redis / Celery
    redis_url: str = "redis://localhost:6379/0"

    # MinIO 对象存储
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket_papers: str = "papers"
    minio_bucket_figures: str = "figures"
    minio_bucket_provenance: str = "provenance"
    minio_secure: bool = False

    # LLM
    litellm_model: str = "openai/gpt-4o-mini"
    litellm_cheap_model: str = "openai/gpt-4o-mini"  # 量大/结构化降本模型，如 qwen-max
    litellm_vision_model: str = "openai/gpt-4o-mini"  # 多模态模型

    # 外部 API key（可选）
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    dashscope_api_key: str = ""
    s2_api_key: str = ""

    # 评审账号（云端）
    judge_username: str = "judge"
    judge_password: str = "ai4s2024"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
