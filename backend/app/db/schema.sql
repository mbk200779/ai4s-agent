-- AI4S 课题（Project）级数据模型
--
-- 关键设计决策：
-- 1. 最小对象从「一篇论文」上移为「一个课题」：papers.project_id NOT NULL，
--    物理上禁止论文脱离课题裸存。
-- 2. 版本是独立的实体，而不是 arxiv_id 上的一个字符串后缀。同一篇论文的 v1 与 v7
--    有各自的 chunk、参考文献与引用位置，共享同一个 paper 行。
-- 3. 所有溯源所需的定位信息（page / bbox / sentence / section_path）都落在行内，
--    反查链 citation → ref → version → paper → project 不需要再回表拼装。
-- 4. 时间一律存 ISO-8601 UTC 字符串，SQLite 无原生时间类型，
--    但字符串排序等价于时间排序，便于直接比较。
-- 5. JSON 字段（authors / bbox / claims / chunk_ids）以 TEXT 存储，
--    读取端统一走 json.loads，保持 schema 简单、不引入 json1 扩展依赖。

PRAGMA foreign_keys = ON;

-- ---------- 课题 ----------
CREATE TABLE IF NOT EXISTS projects (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    research_question TEXT,
    status            TEXT NOT NULL DEFAULT 'active',   -- active | archived
    created_by        TEXT NOT NULL DEFAULT 'system',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- ---------- 论文（课题下的一个文献条目，与被引次数、外部元数据无关） ----------
CREATE TABLE IF NOT EXISTS papers (
    id         TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    arxiv_id   TEXT,                       -- 本地 PDF 上传时为 upload_<hash>
    title      TEXT NOT NULL,
    authors    TEXT NOT NULL DEFAULT '[]', -- JSON: ["A", "B"]
    year       INTEGER,
    venue      TEXT,                       -- 发表期刊/会议，NeurIPS / ACL ...
    abstract   TEXT,
    doi        TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (project_id, arxiv_id)
);

-- ---------- 论文版本 ----------
-- file_hash 是 PDF 字节 sha256，content_hash 是解析后正文的 sha256。
-- 两者的区别很关键：重新排版但内容相同的 PDF file_hash 不同、content_hash 相同，
-- 是「同一版本」；而 v1→v7 通常两者都变。
CREATE TABLE IF NOT EXISTS paper_versions (
    id               TEXT PRIMARY KEY,
    paper_id         TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    version_label    TEXT NOT NULL,                    -- v1 / v7
    file_hash        TEXT NOT NULL,
    content_hash     TEXT,
    ingested_at      TEXT NOT NULL,
    pipeline_version TEXT NOT NULL DEFAULT 'v1.0.0',   -- 解析管线版本，用于复现
    pdf_s3_key       TEXT,
    page_count       INTEGER,
    status           TEXT NOT NULL DEFAULT 'pending',  -- pending | indexed | failed | duplicate
    is_current       INTEGER NOT NULL DEFAULT 1,       -- 该论文当前展示的版本

    -- ---- 异步解析的进度与断点（见 docs/2.7-异步解析与长文档.md）----
    -- status 是"结论"，stage 是"现在走到哪一步"。两者分开是因为前端需要
    -- 在 status 还是 pending 的时候就知道是在下载、在解析第几页、还是在建引用。
    task_id          TEXT,                             -- 当前这次解析的 Celery 任务 id
    stage            TEXT NOT NULL DEFAULT 'queued',    -- queued|downloading|parsing|structuring|indexed|failed|duplicate|reused
    progress_page    INTEGER NOT NULL DEFAULT 0,        -- 已完成页数，进度条分子
    parse_cursor     INTEGER NOT NULL DEFAULT 0,        -- 下一页该从几开始（断点续跑用）
    index_cursor     INTEGER NOT NULL DEFAULT 0,        -- 已写入的片段数，保证 Qdrant 点 ID 跨批次连续
    text_bytes       INTEGER NOT NULL DEFAULT 0,        -- 已追加到正文日志的字节数（断点时为截断点）
    chunk_count      INTEGER NOT NULL DEFAULT 0,
    ref_count        INTEGER NOT NULL DEFAULT 0,
    citation_count   INTEGER NOT NULL DEFAULT 0,
    attempt          INTEGER NOT NULL DEFAULT 0,        -- 已尝试次数，超过 MAX_ATTEMPTS 置 failed
    error_message    TEXT,
    error_trace      TEXT,
    started_at       TEXT,
    finished_at      TEXT,
    reused_from      TEXT,                              -- file_hash 命中时复用的源版本 id
    UNIQUE (paper_id, version_label),
    UNIQUE (paper_id, file_hash)
);

-- ---------- 文本块 ----------
-- chunk_index 在 version 内唯一且稳定，Qdrant 里的点 ID 由它派生，
-- 迁移或重建索引时可以一一对上，不会丢溯源。
CREATE TABLE IF NOT EXISTS chunks (
    id              TEXT PRIMARY KEY,
    version_id      TEXT NOT NULL REFERENCES paper_versions(id) ON DELETE CASCADE,
    chunk_index     INTEGER NOT NULL,
    page            INTEGER,
    bbox            TEXT,                              -- JSON: [x0, y0, x1, y1]
    section_path    TEXT,                              -- "3 Method > 3.2 Scaled Attention"
    chunk_type      TEXT NOT NULL DEFAULT 'text',      -- text|table|figure|formula|reference
    table_id        TEXT,                              -- 表格编号，如 "Table 2"
    latex           TEXT,                              -- formula 类型专用
    text            TEXT NOT NULL,
    token_count     INTEGER,
    qdrant_point_id TEXT,                              -- 与向量库对应，便于回查
    anchors         TEXT,                              -- JSON: 句子级锚点 {v,page,page_size,text_layer,precision,items:[{ref_id,start,end,rects,bbox}]}
    created_at      TEXT NOT NULL,
    UNIQUE (version_id, chunk_index)
);

-- ---------- 参考文献条目（论文自己列出的那些） ----------
-- matched_paper_id 指向本课题内已入库的论文；未入库时为 NULL，
-- 后续「去重 / 补库」可以直接按 (parsed_title, parsed_year) 回填。
CREATE TABLE IF NOT EXISTS paper_refs (
    id              TEXT PRIMARY KEY,
    version_id      TEXT NOT NULL REFERENCES paper_versions(id) ON DELETE CASCADE,
    ref_index       INTEGER NOT NULL,                  -- 参考文献序号 [12]
    raw_text        TEXT NOT NULL,                     -- 原文条目
    parsed_title    TEXT,
    parsed_authors  TEXT,                              -- JSON
    parsed_year     INTEGER,
    matched_paper_id TEXT REFERENCES papers(id) ON DELETE SET NULL,
    match_score     REAL,                              -- 0..1，模糊匹配置信度
    match_method    TEXT,                              -- arxiv_id|doi|title_fuzzy|none
    UNIQUE (version_id, ref_index)
);

-- ---------- 正文里的引用位置（哪一页、哪个框、哪句话） ----------
-- 一个 ref 可以在正文被引用多次，所以 citations 是 N:1 指向 paper_refs。
CREATE TABLE IF NOT EXISTS citations (
    id         TEXT PRIMARY KEY,
    ref_id     TEXT NOT NULL REFERENCES paper_refs(id) ON DELETE CASCADE,
    chunk_id   TEXT REFERENCES chunks(id) ON DELETE SET NULL,
    page       INTEGER,
    bbox       TEXT,
    sentence   TEXT NOT NULL,                          -- 所在句子原文
    created_at TEXT NOT NULL
);

-- ---------- 检索快照（Answer 的可复现依据） ----------
CREATE TABLE IF NOT EXISTS retrieval_snapshots (
    id         TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    query      TEXT NOT NULL,
    chunk_ids  TEXT NOT NULL DEFAULT '[]',             -- JSON: 命中的 chunk id 及顺序
    created_at TEXT NOT NULL
);

-- ---------- 回答 ----------
-- claims 里每条都是 {text, citations:[citation_id], confidence}，
-- 引用指向 citations 表而不是字符串编号，避免「重排后编号错位」。
CREATE TABLE IF NOT EXISTS answers (
    id                    TEXT PRIMARY KEY,
    project_id            TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    question              TEXT NOT NULL,
    claims                TEXT NOT NULL DEFAULT '[]',
    model_version         TEXT,
    prompt_version        TEXT,
    retrieval_snapshot_id TEXT REFERENCES retrieval_snapshots(id) ON DELETE SET NULL,
    created_at            TEXT NOT NULL
);

-- ---------- 正文指纹（去重与关系判定的输入） ----------
-- 为什么按 version 存而不是按 paper 存：同一工作的 v1 和 v7 是两个不同的正文，
-- 判断"是不是同一工作"要看论文级元数据，判断"是不是同一份正文"必须看版本级指纹。
-- 文件名、PDF 字节 hash 都不进这张表：改名重排的同一份稿子 file_hash 会变，
-- 但它该被判成同一篇。
CREATE TABLE IF NOT EXISTS paper_fingerprints (
    version_id      TEXT PRIMARY KEY REFERENCES paper_versions(id) ON DELETE CASCADE,
    -- 64 位 simhash（hex，16 字符），对正文做 3-gram 加权后投影。
    -- 只作粗粒度旁证：整篇 simhash 对长度敏感（v1→v7 差 12 位，看着"不太像"），
    -- 真正的判据是下面的 MinHash 重合率。
    simhash         TEXT NOT NULL,
    -- MinHash（KMV）草图：正文 3-gram 集合里最小的 128 个 64 位哈希，JSON 数组。
    -- 用来估两篇正文的集合重合率，这是"是不是同一份正文"的主判据。
    minhash         TEXT NOT NULL DEFAULT '[]',
    shingle_count   INTEGER NOT NULL DEFAULT 0,
    -- 段落级 simhash 列表（JSON ["hex", ...]）：用来数"多少段能在对方找到近同段"，
    -- 这是"相似段落数"这个依据的唯一来源，比整篇 simhash 的汉明距离可靠得多
    para_hashes     TEXT NOT NULL DEFAULT '[]',
    para_count      INTEGER NOT NULL DEFAULT 0,
    -- 摘要 / 首段的 e5 向量（JSON float[]）。语义相似度只在这里用，
    -- 检索仍然走 Qdrant——这里要的是"两段文本像不像"，不是"哪段最相关"
    abstract_emb    TEXT,
    lead_emb        TEXT,
    abstract_head   TEXT,                       -- 摘要前 300 字，界面直接展示
    lead_head       TEXT,                       -- 首段前 300 字
    body_chars      INTEGER NOT NULL DEFAULT 0,
    section_count   INTEGER NOT NULL DEFAULT 0,
    source          TEXT NOT NULL DEFAULT 'pdf',-- pdf | chunks（老数据回填）
    created_at      TEXT NOT NULL
);

-- ---------- 去重判定记录 ----------
-- pair_key = 两个正文指纹的无序对（小在前），这是"以后不再重复打扰"的键：
-- 同一份内容再传一次，simhash 一样，pair_key 一样，直接套用上次的选择。
-- 用 version_id 做键是不行的——重新上传会产生新的 version_id，但内容没变。
CREATE TABLE IF NOT EXISTS dedupe_decisions (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    pair_key        TEXT NOT NULL,
    relation        TEXT NOT NULL,                  -- version_of | possible_duplicate | related
    action          TEXT NOT NULL,                  -- keep_new | keep_both | link_only
    confidence      REAL NOT NULL DEFAULT 0,
    evidence        TEXT NOT NULL DEFAULT '{}',     -- JSON：判定依据（段落数/标题/作者/余弦）
    new_simhash     TEXT,
    peer_simhash    TEXT,
    new_version_id  TEXT,
    peer_version_id TEXT,
    new_label       TEXT,
    peer_label      TEXT,
    suppress        INTEGER NOT NULL DEFAULT 1,     -- 1=以后同样的一对不再问
    decided_by      TEXT NOT NULL DEFAULT 'user',   -- user | remembered（套用上次选择）
    created_at      TEXT NOT NULL,
    UNIQUE (project_id, pair_key)
);

-- ---------- 索引版本（向量索引的 pipeline 版本，见 docs/2.17） ----------
-- 为什么版本化的单位是「一个 collection」而不是 payload 上的一个字段：
-- 换 embedding 模型时向量维度、相似度尺度、稀疏词表全都变了，两代向量
-- 放进同一个 collection 里既建不了（named vector 的 size 是 collection 级），
-- 也没法比较（距离不可比）。所以一个版本 = 一个 collection，切流 = 换指针。
--
-- 为什么存量数据不搬家：`ai4s_papers` 里的向量是版本化之前写进去的，
-- 它的签名（用了哪个模型、截断多少字符）已经不可考。给它编一个签名是伪造，
-- 所以单独登记成 origin='legacy'，让它继续服务，直到第一次重建完成再退役。
CREATE TABLE IF NOT EXISTS index_versions (
    pipeline_version TEXT PRIMARY KEY,                  -- idx-v1.0.0-8f3c1a2b
    collection       TEXT NOT NULL,                     -- Qdrant collection 名
    signature        TEXT NOT NULL DEFAULT '{}',        -- JSON：切分/模型/截断/schema
    fingerprint      TEXT NOT NULL DEFAULT '',          -- signature 的 sha1 前 12 位
    status           TEXT NOT NULL DEFAULT 'building',  -- building|ready|active|retired|failed
    origin           TEXT NOT NULL DEFAULT 'built',     -- built|legacy
    points           INTEGER NOT NULL DEFAULT 0,
    chunk_count      INTEGER NOT NULL DEFAULT 0,
    papers           INTEGER NOT NULL DEFAULT 0,
    build_id         TEXT,
    note             TEXT,
    created_at       TEXT NOT NULL,
    built_at         TEXT,
    activated_at     TEXT,
    retired_at       TEXT
);

-- ---------- 重建任务（可中断、可续跑） ----------
-- 断点 = (cursor_version, cursor_offset)：处理到哪个版本、该版本内第几个 chunk。
-- 不存"已完成多少条"这种累计量，因为它推不出下一批该读哪儿；
-- 崩溃后重跑必须能从一行精确地接上。
CREATE TABLE IF NOT EXISTS index_builds (
    id              TEXT PRIMARY KEY,
    target_version  TEXT NOT NULL,                      -- 目标 pipeline_version
    project_id      TEXT,                               -- NULL = 全库
    status          TEXT NOT NULL DEFAULT 'queued',     -- queued|running|cancelling|cancelled|done|failed
    cursor_version  TEXT,                               -- 下一个要处理的 version_id
    cursor_offset   INTEGER NOT NULL DEFAULT 0,         -- 该 version 内已提交的 chunk 偏移
    total_chunks    INTEGER NOT NULL DEFAULT 0,
    done_chunks     INTEGER NOT NULL DEFAULT 0,
    failed_chunks   INTEGER NOT NULL DEFAULT 0,
    points_written  INTEGER NOT NULL DEFAULT 0,
    batch_size      INTEGER NOT NULL DEFAULT 64,
    elapsed_s       REAL NOT NULL DEFAULT 0,
    peak_rss_mb     REAL,
    cpu_s           REAL,                               -- 进程 CPU 时间增量
    error_message   TEXT,
    options         TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    finished_at     TEXT
);

-- ---------- 切流规则 ----------
-- 一条 global 行 + 若干 project 行。global 行同时表达"灰度目标"和"灰度外回落"：
--   percent=0   → 全量切到 pipeline_version（stable_version 无意义）
--   0<percent<100 → pipeline_version 吃 percent%，其余走 stable_version
-- 这样回滚就是改一行（pipeline_version ← stable_version），不需要搬数据。
CREATE TABLE IF NOT EXISTS index_routing (
    id               TEXT PRIMARY KEY,
    scope            TEXT NOT NULL,                     -- global|project
    project_id       TEXT NOT NULL DEFAULT '',          -- global 行为空串（NULL 在 UNIQUE 里不互斥）
    pipeline_version TEXT NOT NULL,
    stable_version   TEXT,                              -- 灰度外回落到哪一版
    percent          INTEGER NOT NULL DEFAULT 0,
    enabled          INTEGER NOT NULL DEFAULT 1,
    note             TEXT,
    updated_at       TEXT NOT NULL,
    UNIQUE (scope, project_id)
);

-- ---------- 索引 ----------
CREATE INDEX IF NOT EXISTS idx_papers_project      ON papers(project_id);
CREATE INDEX IF NOT EXISTS idx_papers_arxiv        ON papers(arxiv_id);
CREATE INDEX IF NOT EXISTS idx_versions_paper      ON paper_versions(paper_id);
CREATE INDEX IF NOT EXISTS idx_versions_filehash   ON paper_versions(file_hash);
CREATE INDEX IF NOT EXISTS idx_chunks_version      ON chunks(version_id);
CREATE INDEX IF NOT EXISTS idx_chunks_qdrant       ON chunks(qdrant_point_id);
CREATE INDEX IF NOT EXISTS idx_refs_version        ON paper_refs(version_id);
CREATE INDEX IF NOT EXISTS idx_refs_matched        ON paper_refs(matched_paper_id);
CREATE INDEX IF NOT EXISTS idx_citations_ref       ON citations(ref_id);
CREATE INDEX IF NOT EXISTS idx_citations_chunk     ON citations(chunk_id);
CREATE INDEX IF NOT EXISTS idx_answers_project     ON answers(project_id);
-- 进度查询按 task_id 查单行；file_hash 去重按 (hash, status) 查可复用的源版本
CREATE INDEX IF NOT EXISTS idx_versions_task       ON paper_versions(task_id);
CREATE INDEX IF NOT EXISTS idx_versions_hash_state ON paper_versions(file_hash, status);
CREATE INDEX IF NOT EXISTS idx_dedupe_pair           ON dedupe_decisions(project_id, pair_key);

-- ---------- 反查视图 ----------
-- 自检标准里那条「从任意一条引用反查到哪篇论文的哪个版本、第几页、哪一句」
-- 直接查这个视图即可，不用调用方自己 join 五张表。
CREATE VIEW IF NOT EXISTS v_citation_provenance AS
SELECT
    c.id            AS citation_id,
    c.sentence      AS sentence,
    c.page          AS page,
    c.bbox          AS bbox,
    r.ref_index     AS ref_index,
    r.raw_text      AS ref_raw_text,
    r.matched_paper_id AS matched_paper_id,
    pv.id           AS version_id,
    pv.version_label AS version_label,
    pv.ingested_at  AS ingested_at,
    p.id            AS paper_id,
    p.title         AS paper_title,
    p.arxiv_id      AS paper_arxiv_id,
    pj.id           AS project_id,
    pj.name         AS project_name,
    ck.id           AS chunk_id,
    ck.section_path AS section_path,
    ck.text         AS chunk_text
FROM citations c
JOIN paper_refs r      ON r.id = c.ref_id
JOIN paper_versions pv ON pv.id = r.version_id
JOIN papers p          ON p.id = pv.paper_id
JOIN projects pj       ON pj.id = p.project_id
LEFT JOIN chunks ck    ON ck.id = c.chunk_id;
