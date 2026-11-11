"""
评测跑批：三组基线 + 本系统，一键跑完，输出固定格式，可重放校验。

为什么要有这个文件，而不是"每次临时写个脚本调一下接口"：
一次评测的价值不在数字本身，而在**别人能不能复算出同一组数字**。
所以这里做的每一件事都在服务这一条：

  1. 配置写死成 CONFIGS 常量，`dense/sparse/hybrid` 三种基线跑的是**同一份检索代码**
     的不同配置（`RetrievalService.channels`），不是三套实现。若基线另写一套，
     比出来的差异里就混进了实现差异，证明不了方法好坏。
  2. 每次运行落一个 run 目录：manifest（索引版本/快照/prompt 版本与 hash/seed/model）、
     retrieval.jsonl（逐题召回快照）、answers.jsonl（逐题生成结果）、metrics.json、
     report.md、review.csv（人工抽检表）。
  3. `verify <run_id>` 用同一份数据集重跑检索，与存档逐题比对 chunk_id 序列。
     这就是"同输入同输出"的**可执行定义**——不是口头保证。

用法（容器内）：
    python eval/baseline/run_baseline.py validate
    python eval/baseline/run_baseline.py run --tag demo
    python eval/baseline/run_baseline.py verify <run_id>

设计取舍见 docs/2.18-评测基线与可复现实验.md。
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import csv
import hashlib
import json
import os
import random
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict

sys.path.insert(0, "/app")

import judge  # noqa: E402  同目录，靠 sys.path[0]（脚本所在目录）导入

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATASET_PATH = os.path.join(BASE_DIR, "dataset", "questions.jsonl")
TRANSLATION_PATH = os.path.join(BASE_DIR, "dataset", "translations.json")
RUNS_DIR = os.path.join(BASE_DIR, "runs")

TOP_K = 5          # 指标口径：Recall@5，所以所有配置都取 5 条
RRF_K = 60
DEFAULT_SEED = 20261110


# ---------------------------------------------------------------------------
# 配置：三组基线 + 本系统
# ---------------------------------------------------------------------------

@dataclass
class Config:
    """
    一个待评测配置。

    所有基线都 `force_type="generic"`：这是"只用一路或两路召回、然后直接取 top-k"，
    不做查询理解、不按类型换策略。基线本来就该是这套朴素定义，
    否则"我们的方案比基线好"就变成了"我们的方案比另一个我们的方案好"。
    """

    name: str
    label: str
    channels: str = "hybrid"
    force_type: str | None = "generic"
    rerank: bool = False
    numeric: bool = False
    generate: bool = False

    def dump(self) -> dict:
        return asdict(self)


CONFIGS: list[Config] = [
    Config("dense", "纯向量（dense only）", channels="dense", generate=True),
    Config("sparse", "纯 BM25（sparse only）", channels="sparse", generate=True),
    Config("hybrid", "混合召回（dense+sparse RRF）", channels="hybrid", generate=True),
    Config("hybrid_rerank", "混合 + 精排（baseline 最强组）", channels="hybrid", rerank=True, generate=True),
    Config(
        "system", "本系统（查询理解 + 分流 + 规则召回 + 精排）",
        channels="hybrid", force_type=None, rerank=True, numeric=True, generate=True,
    ),
]


# ---------------------------------------------------------------------------
# 数据集
# ---------------------------------------------------------------------------

def load_dataset() -> list[dict]:
    rows = []
    with open(DATASET_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def dataset_sha1() -> str:
    with open(DATASET_PATH, "rb") as fh:
        return hashlib.sha1(fh.read()).hexdigest()


def load_translations() -> dict:
    if not os.path.exists(TRANSLATION_PATH):
        return {}
    with open(TRANSLATION_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def corpus_rows() -> list[dict]:
    """当前索引里的正文（直接读 DB，与入库时同一份文本）。"""
    from app.core.config import settings

    conn = sqlite3.connect(f"{settings.db_path}", timeout=15)
    try:
        cur = conn.execute(
            """
            SELECT p.arxiv_id, ch.page, ch.chunk_index, ch.text
            FROM chunks ch
            JOIN paper_versions pv ON pv.id = ch.version_id
            JOIN papers p ON p.id = pv.paper_id
            WHERE pv.is_current = 1 AND ch.text IS NOT NULL
            """
        )
        return [
            {"arxiv_id": r[0], "page": r[1], "chunk_index": r[2], "text": r[3]}
            for r in cur.fetchall()
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 校验数据集：gold 引句必须真的在库里，不可答题的判别词必须真的零命中
# ---------------------------------------------------------------------------

def cmd_validate(_args) -> int:
    rows = load_dataset()
    corpus = corpus_rows()
    by_paper: dict[str, list[dict]] = {}
    for c in corpus:
        by_paper.setdefault(c["arxiv_id"], []).append(c)
    all_text = judge.norm(" ".join(c["text"] for c in corpus))

    problems: list[str] = []
    ids = [r["id"] for r in rows]
    if len(ids) != len(set(ids)):
        problems.append(f"题号重复：{len(ids) - len(set(ids))} 个")
    for r in rows:
        if r["expect"] == "answer" and not r.get("gold"):
            problems.append(f"{r['id']} 标为可答但没有 gold")
        if r["expect"] == "refuse" and not r.get("absent_terms"):
            problems.append(f"{r['id']} 标为不可答但没有 absent_terms（无法证伪）")

    print(f"数据集：{len(rows)} 题 · 语料 {len(corpus)} 段 / {len(by_paper)} 篇")
    print("-" * 78)
    for r in rows:
        tag = r["id"]
        if r["expect"] == "answer":
            ok = 0
            for g in r["gold"]:
                cands = by_paper.get(g["arxiv_id"]) or []
                hit = [c for c in cands if judge.quote_hit(g["quote"], c["text"])]
                if hit:
                    ok += 1
                    pages = sorted({c["page"] for c in hit})
                    flag = "" if g["page"] in pages else f"  ⚠ 页码不符（实际 {pages}）"
                    if flag:
                        problems.append(f"{tag} 页码不符：标 {g['page']}，实际 {pages}")
                    print(f"  {tag:5s} ✓ p{g['page']:<3d} \"{g['quote'][:64]}\"{flag}")
                else:
                    problems.append(f"{tag} gold 引句在库内找不到：{g['quote'][:60]}")
                    print(f"  {tag:5s} ✗ 引句未命中 \"{g['quote'][:60]}\"")
            if ok != len(r["gold"]):
                print(f"  {tag:5s} → {ok}/{len(r['gold'])} 条 gold 可用")
        else:
            leaked = [t for t in r["absent_terms"] if judge.norm(t) in all_text]
            if leaked:
                problems.append(f"{tag} 判别词在库内有命中，此题不能算'不可答'：{leaked}")
                print(f"  {tag:5s} ✗ 判别词泄漏 {leaked}")
            else:
                print(f"  {tag:5s} ✓ 不可答（0 命中：{', '.join(r['absent_terms'])}）")

    print("-" * 78)
    n_ans = sum(1 for r in rows if r["expect"] == "answer")
    print(f"可答 {n_ans} · 不可答 {len(rows) - n_ans}")
    dist: dict[str, int] = {}
    for r in rows:
        dist[r["type"]] = dist.get(r["type"], 0) + 1
    print("题型分布：" + " · ".join(f"{k} {v}" for k, v in sorted(dist.items())))
    if problems:
        print(f"\n✗ {len(problems)} 个问题：")
        for p in problems:
            print("   -", p)
        return 1
    print("\n✓ 数据集校验通过")
    return 0


# ---------------------------------------------------------------------------
# 固定译文
# ---------------------------------------------------------------------------

def cmd_translate(args) -> int:
    """
    把中文题问一次翻译成英文，存进 dataset/translations.json。

    为什么要存成文件而不是每次现翻：翻译也是一次 LLM 调用，同一句中文两次
    可能译出两句英文，检索结果随之漂移。那是**翻译的方差**，不是检索的方差。
    把译文固定下来，评测比较的才是检索方法；也让"同输入同输出"有一个
    真正稳定的输入定义。缺译文的题会退回原句检索（会被记录下来）。
    """
    from app.services.llm_client import LLMClient

    rows = load_dataset()
    existing = load_translations()
    llm = LLMClient()
    missing = [r for r in rows if not existing.get(r["id"])]
    print(f"需要译文 {len(missing)} 题（已有 {len(existing)}）")
    for r in missing:
        if r.get("lang") == "en":
            existing[r["id"]] = r["question"]
            continue
        try:
            out = llm.chat(
                f"Translate this research question to English:\n\n{r['question']}",
                system_key="translate_system",
            ).strip()
        except Exception as exc:
            print(f"  {r['id']} 翻译失败：{exc}")
            continue
        existing[r["id"]] = out
        print(f"  {r['id']}: {r['question']} → {out}")
    with open(TRANSLATION_PATH, "w", encoding="utf-8") as fh:
        json.dump(dict(sorted(existing.items())), fh, ensure_ascii=False, indent=1)
    print(f"已写入 {TRANSLATION_PATH}（{len(existing)} 条）")
    return 0


# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------

@dataclass
class RunContext:
    run_id: str
    run_dir: str
    seed: int
    configs: list[Config]
    dataset: list[dict]
    translations: dict
    workers: int = 4
    started_at: str = ""
    retrievals: list[dict] = field(default_factory=list)
    answers: list[dict] = field(default_factory=list)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# 决定检索行为的那几个文件。容器里通常没有 .git，`git rev-parse` 只会返回 unknown，
# 而"这次跑的是哪份代码"是复现记录里最不能缺的一项——所以就按文件内容取指纹。
# 它的另一个用处：两次运行指标变了、但 code_sha1 没变，问题就不在代码里。
CODE_FILES = [
    "app/services/retrieval_service.py",
    "app/services/rag/strategies.py",
    "app/services/rag/retrieve.py",
    "app/services/rag/rerank.py",
    "app/services/rag/confidence.py",
    "app/services/rag/uncertainty.py",
    "app/services/rag/answer.py",
    "app/services/qa_service.py",
    "app/services/index_version.py",
]


def code_sha1() -> str:
    h = hashlib.sha1()
    for rel in CODE_FILES:
        try:
            with open(os.path.join("/app", rel), "rb") as fh:
                h.update(rel.encode("utf-8"))
                h.update(fh.read())
        except OSError:
            h.update(f"{rel}:missing".encode("utf-8"))
    return h.hexdigest()


def _git_rev() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd="/app",
            capture_output=True, text=True, timeout=5,
        ).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def build_manifest(ctx: RunContext, svc, prompt_hash: str) -> dict:
    from app.core.config import settings
    from app.services import index_version as index_version_lib
    from app.services.rag.rerank import index_snapshot_id
    from app.services.rag.taxonomy import RERANK_TOP_N, RRF_MIN_SCORE

    index = index_version_lib.resolve()
    store = svc.rag.retrieval.vector_store
    counts = store.client.count(collection_name=store.chunk_collection, exact=True)
    return {
        "run_id": ctx.run_id,
        "started_at": ctx.started_at,
        "finished_at": _now(),
        "dataset": {
            "path": os.path.relpath(DATASET_PATH, "/app"),
            "sha1": dataset_sha1(),
            "n": len(ctx.dataset),
            "by_type": _count(ctx.dataset, "type"),
            "by_expect": _count(ctx.dataset, "expect"),
            "translations_sha1": _sha1_file(TRANSLATION_PATH),
        },
        "index": {
            "pipeline_version": index.get("pipeline_version"),
            "collection": index.get("collection"),
            "points": counts.count,
            "snapshot_id": index_snapshot_id(store),
            "signature": index.get("signature"),
        },
        "prompt": {
            "version": settings.prompt_version,
            # 版本号是人写的承诺，hash 是机器算的事实：改 prompt 忘了 bump 版本时，
            # 只有 hash 会变。两次运行"版本号一样但结果不一样"的争论，靠它终结。
            "system_sha1": prompt_hash,
        },
        "models": {
            # 从索引签名取，而不是另写一份常量：签名里那两项就是真正在跑的模型，
            # 写两处只改一处的话，报告会声称用了一个模型、实际用的是另一个。
            "dense": (index.get("signature") or {}).get("dense_model"),
            "sparse": (index.get("signature") or {}).get("sparse_model"),
            "rerank": settings.rerank_model,
        },
        "llm": {
            "model": settings.litellm_model,
            "cheap_model": settings.litellm_cheap_model,
            "temperature": settings.temperature,
        },
        "retrieval": {
            "top_k": TOP_K, "rrf_k": RRF_K, "rrf_min_score": RRF_MIN_SCORE,
            "rerank_top_n": RERANK_TOP_N,
            "channels_default": "hybrid",
        },
        "seed": ctx.seed,
        "configs": [c.dump() for c in ctx.configs],
        "code": {"git_rev": _git_rev(), "code_sha1": code_sha1()},
    }


def _count(rows: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for r in rows:
        out[r[key]] = out.get(r[key], 0) + 1
    return dict(sorted(out.items()))


def _sha1_file(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        return hashlib.sha1(fh.read()).hexdigest()


def prompt_sha1() -> str:
    """把实际会用到的 system prompt 拼起来取指纹（翻译 + 作答两条）。"""
    from app.core.config import settings
    from app.services.llm_client import LLMClient

    table = LLMClient.PROMPTS.get(settings.prompt_version, LLMClient.PROMPTS["v1.0.0"])
    raw = "\n---\n".join(f"{k}={table[k]}" for k in ("qa_system", "translate_system") if k in table)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def run_one_retrieval(
    svc, record: dict, cfg: Config, translations: dict, index_version: str | None = None
) -> dict:
    """跑一题一路配置，返回可直接落盘的记录。"""
    qid = record["id"]
    search_text = translations.get(qid) or None
    route = svc.rag.route(
        record["question"], search_text=search_text, top_k=TOP_K, rrf_k=RRF_K,
        force_type=cfg.force_type, rerank=cfg.rerank, numeric=cfg.numeric,
        index_version=index_version,
    )
    results = [
        {
            "rank": i,
            "chunk_id": r.chunk_id,
            "arxiv_id": r.arxiv_id,
            "page": r.page,
            "score": round(r.score or 0.0, 6),
            "raw": round(r.raw_score, 6) if r.raw_score is not None else None,
            "source": r.hit_source,
        }
        for i, r in enumerate(route.results, start=1)
    ]
    judged = judge.score_question({**record, "k": TOP_K}, [
        {"arxiv_id": r.arxiv_id, "text": r.text} for r in route.results
    ])
    trace = route.trace or {}
    top_raw = (trace.get("recall") or {}).get("top_raw_score")
    threshold = route.reject_threshold
    # 与 QAService._is_weak 同一判据：空检索，或原始融合分低于该策略的阈值。
    rejected = (not results) or (
        threshold is not None and top_raw is not None and top_raw < threshold
    )
    ratio = round(top_raw / threshold, 4) if (top_raw is not None and threshold) else None
    return {
        "config": cfg.name,
        "id": qid,
        "type": record["type"],
        "expect": record["expect"],
        "question": record["question"],
        "search_text": search_text or record["question"],
        "ranked": results,
        "top_raw": top_raw,
        "threshold": threshold,
        "ratio": ratio,
        "rejected": bool(rejected),
        "strategy": (trace.get("strategy") or {}).get("name"),
        "channels": (trace.get("strategy") or {}).get("channels"),
        "doc_sha1": hashlib.sha1(
            "|".join(r["chunk_id"] for r in results).encode("utf-8")
        ).hexdigest()[:16],
        **judged,
    }


def run_one_answer(
    svc, record: dict, cfg: Config, translations: dict, index_version: str | None = None
) -> dict:
    """跑一题一次生成（含引用核验与三档置信）。"""
    qid = record["id"]
    started = time.perf_counter()
    out = svc.ask(
        record["question"], top_k=TOP_K, rrf_k=RRF_K,
        force_type=cfg.force_type, rerank=cfg.rerank, numeric=cfg.numeric,
        search_text=translations.get(qid) or None,
        index_version=index_version,
    )
    elapsed = round((time.perf_counter() - started) * 1000, 1)
    claims = out.get("claims") or []
    # 两个数必须分开数，混用会让报告出现"引用数 = 48×5 = 240"这种整齐得可疑的数字：
    #   n_sources     = 检索带回几段（top_k 决定的常数，跟答案无关）
    #   n_citations   = 答案里真正被引用的**唯一片段**数（0 到 top_k 之间，逐题不同）
    # 曾经这里取的是 len(out["sources"])，于是五组配置的"引用总数"都精确等于 240，
    # 看起来像"模型给足了引用"，实际只是每题的候选片段数。指标不报错，只是没意义。
    n_sources = len(out.get("sources") or out.get("citations") or [])
    cited_ids = {e.get("chunk_id") for c in claims for e in (c.get("citations") or [])}
    cited_ids.discard(None)
    n_badges = sum(len(c.get("citations") or []) for c in claims)
    cited_claims = [c for c in claims if c.get("citations")]
    return {
        "config": cfg.name,
        "id": qid,
        "type": record["type"],
        "expect": record["expect"],
        "answer": out.get("answer") or "",
        "rejected": bool(out.get("rejected")),
        "reject_reason": out.get("reject_reason"),
        "n_citations": len(cited_ids),
        "n_sources": n_sources,
        "n_badges": n_badges,
        "invalid_citations": out.get("invalid_citations") or [],
        "claim_stats": out.get("claim_stats") or {},
        "n_claims_with_cite": len(cited_claims),
        "confidence": (out.get("confidence") or {}).get("level"),
        "uncertainty": {
            k: bool((out.get("uncertainty") or {}).get(k))
            for k in ("empty", "conflict", "unreadable")
        },
        "claims": [
            {
                "claim_id": c.get("claim_id"),
                "text": c.get("text"),
                "status": c.get("status"),
                "level": c.get("level"),
                "citations": [
                    {
                        "index": e.get("index"),
                        "chunk_id": e.get("chunk_id"),
                        "arxiv_id": e.get("arxiv_id"),
                        "page": e.get("page"),
                        "text": e.get("text"),
                        "status": e.get("status"),
                        "match_score": e.get("match_score"),
                        "sem_score": e.get("sem_score"),
                    }
                    for e in (c.get("citations") or [])
                ],
            }
            for c in claims
        ],
        "latency_ms": elapsed,
    }


def cmd_run(args) -> int:
    from app.core.config import settings
    from app.services.qa_service import QAService

    os.makedirs(RUNS_DIR, exist_ok=True)
    dataset = load_dataset()
    translations = load_translations()
    configs = [c for c in CONFIGS if c.name in args.configs.split(",")]
    if args.no_generate:
        for c in configs:
            c.generate = False

    stamp = time.strftime("%Y%m%dT%H%M%S")
    short = hashlib.sha1(
        f"{dataset_sha1()}|{','.join(c.name for c in configs)}|{args.seed}".encode()
    ).hexdigest()[:6]
    run_id = f"{stamp}-{short}{('-' + args.tag) if args.tag else ''}"
    run_dir = os.path.join(RUNS_DIR, run_id)
    os.makedirs(run_dir, exist_ok=True)

    ctx = RunContext(
        run_id=run_id, run_dir=run_dir, seed=args.seed,
        configs=configs, dataset=dataset, translations=translations,
        workers=args.workers,
    )
    random.seed(args.seed)
    ctx.started_at = _now()

    print(f"run_id : {run_id}")
    print(f"配置   : {', '.join(c.name for c in configs)}")
    print(f"数据   : {len(dataset)} 题（{dataset_sha1()[:12]}）")
    print(f"seed   : {args.seed} · top_k {TOP_K} · rrf_k {RRF_K}")
    print("-" * 78)

    t0 = time.perf_counter()
    svc = QAService()
    # 评测读哪一版索引必须显式固定：默认按灰度比例分桶，跑两次可能读到两代索引，
    # 那所有"差异"都无从解释。--index-version 不传就记录成 auto，并在报告里注明。
    manifest = build_manifest(ctx, svc, prompt_sha1())
    manifest["index"]["pinned"] = args.index_version or "auto"
    print(f"索引   : {manifest['index']['pipeline_version']} "
          f"({manifest['index']['points']} 点, snapshot {manifest['index']['snapshot_id']})")
    print(f"prompt : {manifest['prompt']['version']} · sha {manifest['prompt']['system_sha1'][:12]}")
    print(f"模型   : {manifest['models']['dense']} · rerank {manifest['models']['rerank']}")

    # ---- 阶段 1：检索（不调生成，确定性） ----
    # 串行跑：检索要的是逐题可复算，并发只会让失败更难定位，而它本来就快。
    print("-" * 78)
    jobs = [(r, c) for c in configs for r in dataset]
    for i, (record, cfg) in enumerate(jobs, start=1):
        svc.rag.retrieval.channels = cfg.channels
        rec = run_one_retrieval(svc, record, cfg, translations, args.index_version)
        ctx.retrievals.append(rec)
        if i % 24 == 0:
            print(f"  检索 {i}/{len(jobs)} · {cfg.name} · {record['id']}", flush=True)
    print(f"检索完成：{len(ctx.retrievals)} 次 · {time.perf_counter() - t0:.1f}s")

    # ---- 阶段 2：生成（可关；只为需要引用与拒答证据的配置跑） ----
    gen_cfgs = [c for c in configs if c.generate]
    if gen_cfgs:
        print("-" * 78)
        started = time.perf_counter()

        def _task(pair):
            record, cfg = pair
            try:
                return run_one_answer(svc, record, cfg, translations, args.index_version)
            except Exception as exc:  # 单题失败不终止整轮
                return {
                    "config": cfg.name, "id": record["id"], "type": record["type"],
                    "expect": record["expect"], "answer": "", "rejected": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "n_citations": 0, "invalid_citations": [], "claims": [],
                    "claim_stats": {}, "confidence": None,
                    "uncertainty": {"empty": False, "conflict": False, "unreadable": False},
                    "latency_ms": 0.0,
                }

        # 按配置分组串行，组内并发：`channels` 是服务实例上的可变状态，
        # 若把不同配置混在一个池子里跑，A 线程刚设成 dense、B 线程已经改成 sparse，
        # 出来的数字属于哪个配置都说不清——这种错不报错，只是结果无法解释。
        for cfg in gen_cfgs:
            svc.rag.retrieval.channels = cfg.channels
            pairs = [(r, cfg) for r in dataset]
            done = 0
            with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
                for rec in pool.map(_task, pairs):
                    ctx.answers.append(rec)
                    done += 1
                    if done % 8 == 0:
                        print(f"  生成 {cfg.name} {done}/{len(pairs)} · {rec['id']} "
                              f"· {time.perf_counter() - started:.0f}s", flush=True)
        svc.rag.retrieval.channels = "hybrid"
        print(f"生成完成：{len(ctx.answers)} 次 · {time.perf_counter() - started:.1f}s")

    # ---- 落盘 ----
    with open(os.path.join(run_dir, "retrieval.jsonl"), "w", encoding="utf-8") as fh:
        for rec in ctx.retrievals:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if ctx.answers:
        with open(os.path.join(run_dir, "answers.jsonl"), "w", encoding="utf-8") as fh:
            for rec in ctx.answers:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    metrics = compute_metrics(ctx)
    # 生成失败必须显式失败，不能"跑完即通过"：某次 provider 欠费会让 240 次生成
    # 全部抛错，而错误被逐题吞掉之后，报告会安静地显示"引用 0 条、有效率 100%"——
    # 看起来像模型不给引用，实际是一次没跑成。这种"绿着的红灯"是最坏的一类。
    total_err = sum(
        b.get("generation", {}).get("errors", 0) for b in metrics["configs"].values()
    )
    total_gen = sum(
        b.get("generation", {}).get("n", 0) for b in metrics["configs"].values()
    )
    manifest["status"] = "ok"
    if total_err:
        manifest["status"] = "degraded" if total_err > total_gen * 0.1 else "partial_errors"
        manifest["generation_errors"] = total_err
        print(f"\n⚠ 生成失败 {total_err}/{total_gen} 次（status={manifest['status']}）"
              "—— 引用类指标不可用，别拿这份报告下结论")
    manifest["finished_at"] = _now()
    manifest["elapsed_s"] = round(time.perf_counter() - t0, 1)
    manifest["files"] = {
        name: _sha1_file(os.path.join(run_dir, name))
        for name in ("retrieval.jsonl", "answers.jsonl")
        if os.path.exists(os.path.join(run_dir, name))
    }
    with open(os.path.join(run_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(run_dir, "metrics.json"), "w", encoding="utf-8") as fh:
        json.dump(metrics, fh, ensure_ascii=False, indent=2)

    write_review_sheet(ctx, run_dir)
    write_report(manifest, metrics, run_dir)
    with open(os.path.join(RUNS_DIR, "LATEST"), "w", encoding="utf-8") as fh:
        fh.write(run_id + "\n")

    print("-" * 78)
    print(f"完成：{run_dir}")
    print(f"总耗时 {manifest['elapsed_s']}s")
    print_main_table(metrics)
    return 1 if manifest["status"] == "degraded" else 0


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------

def compute_metrics(ctx: RunContext) -> dict:
    out: dict = {"run_id": ctx.run_id, "seed": ctx.seed, "configs": {}}
    for cfg in ctx.configs:
        rows = [r for r in ctx.retrievals if r["config"] == cfg.name]
        branch = {"overall": judge.aggregate(rows), "by_type": {}}
        for t in sorted({r["type"] for r in rows}):
            branch["by_type"][t] = judge.aggregate([r for r in rows if r["type"] == t])

        gen = [a for a in ctx.answers if a["config"] == cfg.name]
        if gen:
            answered = [a for a in gen if not a["rejected"] and a.get("answer")]
            # 只看"带引用的句子"：没有角标的句子属于"没给出处"，是另一个问题
            # （07 的 claim 切分把它算作 unsupported）。混在一起会让
            # "引用准确率"变成"引用覆盖率 + 准确率"的混合物。
            n_claims = sum(a.get("n_claims_with_cite", 0) for a in answered)
            bad_claims = sum(
                1 for a in answered for c in (a.get("claims") or [])
                if c.get("citations") and c.get("status") == "unsupported"
            )
            total_cites = sum(a["n_citations"] for a in gen)
            total_badges = sum(a.get("n_badges") or 0 for a in gen)
            invalid = sum(len(a["invalid_citations"]) for a in gen)
            branch["generation"] = {
                "n": len(gen),
                "answered": len(answered),
                "refused": len(gen) - len(answered),
                "claims": n_claims,
                "unsupported_claims": bad_claims,
                "unsupported_rate": round(bad_claims / n_claims, 4) if n_claims else 0.0,
                # 引用数 = 唯一被引片段数；角标数 = 引用次数（同一片段被两句引会算两次）。
                # 有效率的分母必须是角标数：非法引用是"角标解析不出来"，属次数问题。
                "citations": total_cites,
                "citations_per_answer": round(total_cites / len(answered), 2) if answered else 0.0,
                "badges": total_badges,
                "invalid_citations": invalid,
                "citation_validity": round(1 - invalid / total_badges, 4) if total_badges else 0.0,
                "avg_latency_ms": round(
                    sum(a.get("latency_ms", 0.0) for a in gen) / len(gen), 1
                ),
                "errors": sum(1 for a in gen if a.get("error")),
            }
        out["configs"][cfg.name] = branch

    # 生成层的实际拒答与检索层判定是否一致——两个口径不一致就说明中间有别的因素在起作用
    if ctx.answers:
        agree, total = 0, 0
        for a in ctx.answers:
            r = next(
                (x for x in ctx.retrievals if x["config"] == a["config"] and x["id"] == a["id"]),
                None,
            )
            if r and not a.get("error"):
                total += 1
                agree += int(bool(r["rejected"]) == bool(a["rejected"]))
        out["reject_consistency"] = {
            "agree": agree, "total": total,
            "rate": round(agree / total, 4) if total else 0.0,
        }
    return out


def print_main_table(metrics: dict) -> None:
    head = (f"{'配置':<16}{'Recall@5':>10}{'MRR@5':>9}{'nDCG@5':>9}{'覆盖@5':>9}"
            f"{'误拒率':>9}{'正确拒率':>10}{'拒答准确':>10}{'可分性AUC':>11}")
    print(head)
    for name, b in metrics["configs"].items():
        o = b["overall"]
        print(f"{name:<16}{o['recall@5']:>10.4f}{o['mrr@5']:>9.4f}{o['ndcg@5']:>9.4f}"
              f"{o['coverage@5']:>9.4f}{o['misreject_rate']:>9.2%}{o['correct_refuse_rate']:>10.2%}"
              f"{o['reject_accuracy']:>10.2%}{o['separation_auc']:>11.4f}")


# ---------------------------------------------------------------------------
# 人工抽检表
# ---------------------------------------------------------------------------

REVIEW_COLUMNS = [
    "item_id", "config", "qid", "type", "question", "claim", "cited_sentence",
    "arxiv_id", "page", "chunk_id", "auto_status", "match_score", "sem_score",
    "verdict", "note",
]


def write_review_sheet(ctx: RunContext, run_dir: str, n: int = 20) -> None:
    """
    抽检表：一行 = 一处"答案里的某句话 + 它引的原句"。

    为什么抽的是 claim 而不是 citation：citation 只说"检索到了这段"，
    claim 才说"我拿这段支持了这句话"。引用准确率要评的是后者——
    引了一段真实存在的原文去支持一句它并不支持的话，检索指标完全看不出来。
    """
    pool = []
    for a in sorted(ctx.answers, key=lambda x: (x["config"], x["id"])):
        if a.get("error") or a["rejected"]:
            continue
        for c in a.get("claims") or []:
            for e in c.get("citations") or []:
                pool.append({
                    "config": a["config"], "qid": a["id"], "type": a.get("type") or "",
                    "question": next((r["question"] for r in ctx.dataset if r["id"] == a["id"]), ""),
                    "claim": c.get("text") or "",
                    "cited_sentence": e.get("text") or "",
                    "arxiv_id": e.get("arxiv_id") or "",
                    "page": e.get("page") or 0,
                    "chunk_id": e.get("chunk_id") or "",
                    "auto_status": e.get("status") or "",
                    "match_score": e.get("match_score"),
                    "sem_score": e.get("sem_score"),
                })
    if not pool:
        return
    rng = random.Random(ctx.seed)
    k = min(n, len(pool))
    sample = rng.sample(pool, k)
    # 排序只为让表好读，判定与顺序无关（抽样已在上面完成）
    sample.sort(key=lambda x: (x["config"], x["qid"]))
    path = os.path.join(run_dir, "review.csv")
    # 重算指标时不要抹掉已经做过的人工判定：人花了时间读 20 条原文，
    # 而 `recompute` 只是改一下分母。按 (config, qid, claim) 认领回原来的 verdict/note。
    prev: dict[tuple, dict] = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                for old in csv.DictReader(fh):
                    if old.get("verdict"):
                        prev[(old["config"], old["qid"], old["claim"])] = {
                            "verdict": old["verdict"], "note": old.get("note", ""),
                        }
        except Exception:
            prev = {}
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=REVIEW_COLUMNS)
        w.writeheader()
        for i, item in enumerate(sample, start=1):
            row = {**item, "item_id": f"r{i:02d}", "verdict": "", "note": ""}
            kept = prev.get((item["config"], item["qid"], item["claim"]))
            if kept:
                row.update(kept)
            w.writerow({c: row.get(c, "") for c in REVIEW_COLUMNS})
    with open(os.path.join(run_dir, "review_pool.json"), "w", encoding="utf-8") as fh:
        json.dump({"seed": ctx.seed, "pool": len(pool), "sampled": k}, fh, ensure_ascii=False, indent=2)
    summarize_review(path)


VERDICT_JUDGEABLE = ("支持", "部分支持", "不支持")


def summarize_review(csv_path: str) -> dict | None:
    """
    把 review.csv 里的人工判定汇总成 review_summary.json。

    汇总逻辑放进脚本而不是靠一次性的临时命令：这个数会进文档、进答辩材料，
    而"当时那条 shell 命令"既没版本也复现不了。人只负责填 verdict 与 note，
    分母怎么算由代码决定——这是**唯一**能保证下次重算不会悄悄换口径的方式。

    "不适用"单独计数、不进分母：它标的是"这句话不该被判引用对错"
    （关于片段自身的否定陈述、过渡句、claim 被切成的图注残片），
    混进分母等于拿切句质量去扣引用质量。
    """
    if not os.path.exists(csv_path):
        return None
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8-sig")))
    if not rows:
        return None
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.get("verdict") or "未判"] = counts.get(r.get("verdict") or "未判", 0) + 1
    judged = [r for r in rows if r.get("verdict") in VERDICT_JUDGEABLE]
    if not judged:
        return None
    strict = sum(1 for r in judged if r["verdict"] == "支持")
    loose = sum(1 for r in judged if r["verdict"] in ("支持", "部分支持"))
    summary = {
        "sample_size": len(rows),
        "judgeable": len(judged),
        "excluded_not_applicable": counts.get("不适用", 0),
        "unjudged": counts.get("未判", 0),
        "counts": counts,
        "strict_accuracy": round(strict / len(judged), 4),
        "loose_accuracy": round(loose / len(judged), 4),
        "wrong_citation_rate": round(counts.get("不支持", 0) / len(judged), 4),
        "by_config": {
            cfg: {
                "n": len(sub),
                "strict": round(sum(1 for r in sub if r["verdict"] == "支持") / len(sub), 4),
            }
            for cfg, sub in (
                (c, [r for r in judged if r["config"] == c])
                for c in sorted({r["config"] for r in judged})
            )
        },
    }
    with open(csv_path.replace("review.csv", "review_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    return summary


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def write_report(manifest: dict, metrics: dict, run_dir: str) -> None:
    cfg_labels = {c["name"]: c["label"] for c in manifest["configs"]}
    order = [c["name"] for c in manifest["configs"]]
    base = "hybrid_rerank" if "hybrid_rerank" in order else order[-1]
    lines: list[str] = []
    A = lines.append

    A(f"# 评测报告 · {manifest['run_id']}")
    A("")
    A("## 这次跑的是什么")
    A("")
    A("| 项 | 值 |")
    A("|---|---|")
    A(f"| 数据集 | {manifest['dataset']['n']} 题（sha1 `{manifest['dataset']['sha1']}`）|")
    A(f"| 题型分布 | {' · '.join(f'{k} {v}' for k, v in manifest['dataset']['by_type'].items())} |")
    A(f"| 索引版本 | `{manifest['index']['pipeline_version']}` · {manifest['index']['points']} 点 |")
    A(f"| 检索快照 id | `{manifest['index']['snapshot_id']}` |")
    A(f"| Prompt | `{manifest['prompt']['version']}` · sha1 `{manifest['prompt']['system_sha1'][:16]}` |")
    A(f"| 模型 | `{manifest['models']['dense']}` / rerank `{manifest['models']['rerank']}` |")
    A(f"| LLM | `{manifest['llm']['model']}` · temperature {manifest['llm']['temperature']} |")
    A(f"| seed | {manifest['seed']} |")
    A(f"| 口径 | top_k={manifest['retrieval']['top_k']} · rrf_k={manifest['retrieval']['rrf_k']} "
      f"· rrf_min_score={manifest['retrieval']['rrf_min_score']} |")
    A(f"| 代码 | `{manifest['code']['git_rev']}` · 检索代码 sha1 `{manifest['code']['code_sha1'][:12]}` |")
    A(f"| 快照文件 | `manifest.json` / `retrieval.jsonl` / `answers.jsonl` / `review.csv` / `review_summary.json` |")
    A("")

    A("## 检索基线对比")
    A("")
    A("可答集上的检索质量（分母是可答题数量），拒答两侧分开报。")
    A("")
    A("| 配置 | Recall@5 | MRR@5 | nDCG@5 | 覆盖@5 | 误拒率↓ | 正确拒答率↑ | 拒答准确率↑ | 可分性 AUC↑ |")
    A("|---|---|---|---|---|---|---|---|---|")
    for name in order:
        o = metrics["configs"][name]["overall"]
        A(f"| {cfg_labels[name]} | {o['recall@5']:.4f} | {o['mrr@5']:.4f} | {o['ndcg@5']:.4f} "
          f"| {o['coverage@5']:.4f} | {o['misreject_rate']:.2%} | {o['correct_refuse_rate']:.2%} "
          f"| {o['reject_accuracy']:.2%} | {o['separation_auc']:.4f} |")
    A("")

    if base in metrics["configs"]:
        A("### 相对最强基线（" + cfg_labels[base] + "）的增量")
        A("")
        A("| 配置 | ΔRecall@5 | ΔnDCG@5 | Δ覆盖@5 | Δ拒答准确率 |")
        A("|---|---|---|---|---|")
        bo = metrics["configs"][base]["overall"]
        for name in order:
            o = metrics["configs"][name]["overall"]
            A(f"| {cfg_labels[name]} | {o['recall@5'] - bo['recall@5']:+.4f} "
              f"| {o['ndcg@5'] - bo['ndcg@5']:+.4f} "
              f"| {o['coverage@5'] - bo['coverage@5']:+.4f} "
              f"| {o['reject_accuracy'] - bo['reject_accuracy']:+.4f} |")
        A("")

    A("## 分题型（Recall@5）")
    A("")
    types = sorted({t for n in order for t in metrics["configs"][n]["by_type"]})
    A("| 配置 | " + " | ".join(types) + " |")
    A("|---" * (len(types) + 1) + "|")
    for name in order:
        bt = metrics["configs"][name]["by_type"]
        cells = [
            f"{bt[t]['recall@5']:.4f} (n={bt[t]['n_answer']})"
            if t in bt and bt[t]["n_answer"] else "—"
            for t in types
        ]
        A(f"| {cfg_labels[name]} | " + " | ".join(cells) + " |")
    A("")

    gen_rows = [(n, metrics["configs"][n]["generation"]) for n in order
                if metrics["configs"][n].get("generation")]
    if gen_rows:
        A("## 生成层：引用与拒答")
        A("")
        A("| 配置 | 作答 | 拒答 | 被引片段 | 每题被引 | 非法引用 | 引用有效率 | 句级无支撑率 | 平均耗时 |")
        A("|---|---|---|---|---|---|---|---|---|")
        for name, g in gen_rows:
            A(f"| {cfg_labels[name]} | {g['answered']} | {g['refused']} | {g['citations']} "
              f"| {g['citations_per_answer']:.2f} | {g['invalid_citations']} | {g['citation_validity']:.2%} "
              f"| {g['unsupported_rate']:.2%} | {g['avg_latency_ms']:.0f}ms |")
        A("")
        A("「被引片段」= 答案里真正挂过角标的**唯一片段**数（不是检索带回的候选数，"
          "后者等于 48×5=240 且与答案无关）；「每题被引」= 每个作答平均挂了几段。")
        A("「引用有效率」是代码校验（角标是否解析到真实片段，分母是**角标次数**），"
          "「句级无支撑率」是 07 的自动核验（跨语言语义核验 + 词面匹配）判为无支撑的、"
          "**带引用**的句子占比。三者的分母互不相同，不能互相替代；人工抽检见 `review.csv`。")
        A("")
        if "reject_consistency" in metrics:
            rc = metrics["reject_consistency"]
            A(f"检索层与生成层的拒答判定一致率：**{rc['rate']:.2%}**（{rc['agree']}/{rc['total']}）。"
              "不一致的样本通常来自「空检索」这条独立通道，值得逐条看。")
            A("")

    A("## 怎么读这张表")
    A("")
    A("- **Recall@5 是主指标**：它只问「有一条标了 gold 的原文片段进了前 5 吗」，不掺生成质量。")
    A("- **覆盖@5 比 Recall@5 严**：多证据题（对比/综述）要求多条 gold 都进前 5，")
    A("  单路检索在这种题上会明显掉队——一条通道只能带回一篇。")
    A("- **误拒率与正确拒答率必须分开看**：合成一个「拒答准确率」时，一个什么都拒的系统")
    A("  在不可答集上满分、在可答集上全错，合成后仍有 50%。")
    A("- **引用有效率来自代码校验**（角标 ID 是否指向真实片段），不是人判的；")
    A("  人工抽检见 `review.csv`，两边的差异本身就是结论。")
    n_ans = manifest["dataset"]["by_expect"].get("answer", 0)
    A(f"- **最小可检测差异约 {100.0 / n_ans:.1f} 个百分点**（1 道题 = 1/{n_ans}）：")
    A("  可答集只有这么多题，差一个题就是这么大的跳动。表里小于这个幅度的差异")
    A("  不该被当成本事——要么加题，要么别下结论。")
    A("- **可分性 AUC 与阈值无关**：它只问「不可答题的检索分是不是系统性低于可答题」，")
    A("  0.5 表示完全没有信号。阈值怎么定会同时改变误拒率和正确拒答率，AUC 不会——")
    A("  要判断「拒答能力」这件事本身，看这一列；要判断「这个阈值调得怎么样」，看上三列。")
    A("")
    rsum = summarize_review(os.path.join(run_dir, "review.csv"))
    if rsum:
        A("")
        A("## 人工抽检（引用准确率）")
        A("")
        A(f"从抽样池里固定 seed 抽 {rsum['sample_size']} 条，人逐条读被引原文。"
          f"可判 {rsum['judgeable']} 条，「不适用」{rsum['excluded_not_applicable']} 条不进分母。")
        A("")
        A("| 判定 | 条数 |")
        A("|---|---|")
        for k in ("支持", "部分支持", "不支持", "不适用"):
            if k in rsum["counts"]:
                A(f"| {k} | {rsum['counts'][k]} |")
        A("")
        A(f"- **严格准确率 {rsum['strict_accuracy']:.2%}**"
          f"（{rsum['counts'].get('支持', 0)}/{rsum['judgeable']}）")
        A(f"- 宽松（支持 + 部分支持）{rsum['loose_accuracy']:.2%}")
        A(f"- 明确错引率 {rsum['wrong_citation_rate']:.2%}")
        A("")
        A("组间不可比：每组只有几条。逐条判定与理由见 `review.csv` 的 `verdict` / `note`。")
        A("")
    A("## 复现")
    A("")
    A("```bash")
    A(f"python eval/baseline/run_baseline.py verify {manifest['run_id']}   # 重放检索并逐题比对")
    A("```")
    A("")
    with open(os.path.join(run_dir, "report.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


# ---------------------------------------------------------------------------
# 只重算指标，不重跑
# ---------------------------------------------------------------------------

def cmd_review(args) -> int:
    """填完 verdict 后重算汇总，不碰 csv（人的判定是输入，不是派生数据）。"""
    run_dir = os.path.join(RUNS_DIR, args.run_id)
    summary = summarize_review(os.path.join(run_dir, "review.csv"))
    if not summary:
        print("review.csv 里还没有判定（verdict 列全空）")
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["unjudged"]:
        print(f"⚠ 还有 {summary['unjudged']} 条未判，它们不计入任何分母")
    return 0


def cmd_recompute(args) -> int:
    """
    读一次已存档的运行，重算指标与报告，不碰检索、不碰模型。

    存在的理由很实际：指标口径一改（比如"引用数"该按被引用片段数算、
    而不是按检索到的片段数算），重跑一次要四分钟外加 240 次模型调用；
    而原始数据（逐题召回、逐题答案、逐句证据）都在盘上，重算就够了。
    前提是**原始数据没有被改动**——所以这里只做"派生字段 + 汇总"，
    绝不重新生成答案，也不假装重跑过。
    """
    run_dir = os.path.join(RUNS_DIR, args.run_id)
    manifest = json.load(open(os.path.join(run_dir, "manifest.json"), encoding="utf-8"))
    ctx = RunContext(
        run_id=manifest["run_id"], run_dir=run_dir, seed=manifest["seed"],
        configs=[Config(**{k: c[k] for k in c if k in Config.__annotations__})
                 for c in manifest["configs"]],
        dataset=load_dataset(), translations=load_translations(),
    )
    ctx.started_at = manifest.get("started_at", "")
    ctx.retrievals = [json.loads(line) for line in open(
        os.path.join(run_dir, "retrieval.jsonl"), encoding="utf-8") if line.strip()]
    apath = os.path.join(run_dir, "answers.jsonl")
    if os.path.exists(apath):
        ctx.answers = [json.loads(line) for line in open(apath, encoding="utf-8") if line.strip()]

    for a in ctx.answers:
        claims = a.get("claims") or []
        ids = {e.get("chunk_id") for c in claims for e in (c.get("citations") or [])}
        ids.discard(None)
        a["n_citations"] = len(ids)
        a["n_badges"] = sum(len(c.get("citations") or []) for c in claims)
        a["n_claims_with_cite"] = sum(1 for c in claims if c.get("citations"))

    metrics = compute_metrics(ctx)
    manifest["code"] = {"git_rev": _git_rev(), "code_sha1": code_sha1()}
    manifest["recomputed_at"] = _now()
    # manifest 也要落盘：只改 metrics 不写 manifest，会让报告里引用的代码指纹
    # 与 manifest 里的记录对不上——这种"两份记录各说一句"是最难查的一类不一致。
    manifest["files"] = {
        name: _sha1_file(os.path.join(run_dir, name))
        for name in ("retrieval.jsonl", "answers.jsonl")
        if os.path.exists(os.path.join(run_dir, name))
    }
    with open(os.path.join(run_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(run_dir, "metrics.json"), "w", encoding="utf-8") as fh:
        json.dump(metrics, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(run_dir, "answers.jsonl"), "w", encoding="utf-8") as fh:
        for rec in ctx.answers:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    write_review_sheet(ctx, run_dir)
    write_report(manifest, metrics, run_dir)
    print(f"已重算：{run_dir}")
    print_main_table(metrics)
    return 0


# ---------------------------------------------------------------------------
# 重放校验
# ---------------------------------------------------------------------------

def _content_sig(ranked: list[dict]) -> str:
    """
    内容指纹：(论文, 页码, 分数) 的序列。

    为什么"可复现"不能只比 chunk_id：chunk_id 是**标签**，不是内容。
    14 实测过一次——点 ID 映射有个 UUID 连字符 bug，修好之后每条结果的 chunk_id
    从 point-id 变成了 chk_，但召回的片段、顺序、分数一模一样。
    只比 chunk_id 会把这种"标签变了、行为没变"判成不可复现，
    反过来也会把"标签一样、内容被换了"（重建索引后点被复用）判成可复现。
    两个都算：content 判内容，chunk_id 判标签，不一致时分别说清楚。
    """
    raw = "|".join(f"{r.get('arxiv_id')}:{r.get('page')}:{float(r.get('score') or 0):.6f}"
                   for r in ranked)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def cmd_verify(args) -> int:
    """
    重放一次已存档的运行，逐题比对召回序列。

    比的是 chunk_id 序列而不是分数：分数会因浮点顺序、归一化细节有小波动，
    而"同样的问题回到同样的片段、同样的顺序"才是检索可复现的定义。
    """
    from app.services.qa_service import QAService

    run_dir = os.path.join(RUNS_DIR, args.run_id)
    manifest = json.load(open(os.path.join(run_dir, "manifest.json"), encoding="utf-8"))
    if manifest["dataset"]["sha1"] != dataset_sha1():
        print(f"✗ 数据集已变更：存档 {manifest['dataset']['sha1'][:12]} "
              f"≠ 当前 {dataset_sha1()[:12]}；这次比对无意义")
        return 2

    archived = [json.loads(line) for line in open(
        os.path.join(run_dir, "retrieval.jsonl"), encoding="utf-8") if line.strip()]
    dataset = {r["id"]: r for r in load_dataset()}
    translations = load_translations()
    cfgmap = {c["name"]: Config(**{k: c[k] for k in c if k in Config.__annotations__})
              for c in manifest["configs"]}

    svc = QAService()
    store = svc.rag.retrieval.vector_store
    from app.services.rag.rerank import index_snapshot_id

    now_snap = index_snapshot_id(store)
    same_index = now_snap == manifest["index"]["snapshot_id"]
    print(f"存档快照 {manifest['index']['snapshot_id']} · 当前 {now_snap} "
          f"· {'一致' if same_index else '⚠ 不一致（索引已变，结果允许不同）'}")

    diffs, id_diffs, total = [], [], 0
    pin = manifest["index"].get("pinned")
    pinned = None if not pin or pin == "auto" else pin
    for rec in archived:
        cfg = cfgmap[rec["config"]]
        svc.rag.retrieval.channels = cfg.channels
        again = run_one_retrieval(svc, dataset[rec["id"]], cfg, translations, pinned)
        total += 1
        if _content_sig(again["ranked"]) != _content_sig(rec["ranked"]):
            diffs.append((rec["config"], rec["id"],
                          _content_sig(rec["ranked"]), _content_sig(again["ranked"])))
        if again["doc_sha1"] != rec["doc_sha1"]:
            id_diffs.append((rec["config"], rec["id"]))
        if total % 32 == 0:
            print(f"  比对 {total}/{len(archived)}", flush=True)

    print("-" * 78)
    print(f"内容（论文/页码/分数序列）：{total - len(diffs)}/{total} 一致")
    print(f"标签（chunk_id 序列）：    {total - len(id_diffs)}/{total} 一致")
    if id_diffs and not diffs:
        print("  说明：内容完全一致，只是 chunk_id 的**写法**变了（点 ID 映射修复后"
              "由 point-id 变回 chk_）。这属于标签变化，不是结果漂移。")
    if not diffs:
        print(f"✓ {total} 次检索逐题一致")
        return 0
    print(f"{'✗' if same_index else '·'} {len(diffs)}/{total} 次内容不一致")
    for cfg, qid, a, b in diffs[:20]:
        print(f"   {cfg:14s} {qid}: {a} → {b}")
    return 1 if same_index else 0


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="评测基线与可复现实验")
    sub = ap.add_subparsers(dest="cmd")

    v = sub.add_parser("validate", help="校验数据集标注（引句可命中、判别词零命中）")
    v.set_defaults(func=cmd_validate)

    t = sub.add_parser("translate", help="补齐中文题的固定英文译文")
    t.set_defaults(func=cmd_translate)

    r = sub.add_parser("run", help="跑一次完整评测")
    r.add_argument("--configs", default=",".join(c.name for c in CONFIGS))
    r.add_argument("--seed", type=int, default=DEFAULT_SEED)
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--tag", default="")
    r.add_argument("--index-version", default=None, help="固定读哪一版索引（评测必须固定）")
    r.add_argument("--no-generate", action="store_true", help="只跑检索，不调生成")
    r.set_defaults(func=cmd_run)

    rv = sub.add_parser("review", help="把 review.csv 里填好的判定汇总成 review_summary.json")
    rv.add_argument("run_id")
    rv.set_defaults(func=cmd_review)

    rc = sub.add_parser("recompute", help="只重算指标与报告（不重跑检索与生成）")
    rc.add_argument("run_id")
    rc.set_defaults(func=cmd_recompute)

    f = sub.add_parser("verify", help="重放某次运行并逐题比对")
    f.add_argument("run_id")
    f.set_defaults(func=cmd_verify)

    args = ap.parse_args()
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
