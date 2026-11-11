"""
索引 pipeline 版本化：版本标识、collection 命名、灰度切流与回滚。

为什么需要这一层：切分策略或 embedding 模型一变，旧的向量就不是"稍差一点"，
而是**和新的不可比**——维度可能不同（collection 级 named vector 的 size 是定死的）、
稀疏词表变了、相似度尺度变了。没有版本时唯一的选择是"停服全库重灌"，
于是没人敢在演示前一天调参。

这里做的四件事：

1. **版本 = 一个 collection。** 一个 pipeline 签名对应一个 collection，
   切换版本就是换一个指针，不动数据。
2. **存量不搬家。** 版本化之前写在 `ai4s_papers` 里的向量，签名已经不可考，
   给它编一个签名就是伪造——所以单独登记成 `origin='legacy'`，继续服务到退役。
3. **切流按 Project / 按比例，不做全局硬切。** 规则存在 `index_routing`：
   一条 global 行（灰度目标 + 灰度外回落）+ 若干 project 行。
   回滚 = 把 global 行的 pipeline_version 改回 stable_version，O(1)。
4. **写路径不跟灰度。** 重建期间新入库的论文只写稳定版，否则灰度期间
   两个索引的内容开始漂移，A/B 比出来的差异里混进了"旧索引没有新论文"。

与 `paper_versions.pipeline_version` 的区别：那个是**解析**管线（PDF→chunk），
这个是**索引**管线（chunk→向量）。换切分策略改的是后者；换 PyMuPDF 的解析
行为改的是前者。两者可以独立回滚，所以不共用字段。
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from app.core.config import settings
from app.db import dump_json, load_json, new_id, reader, transaction, utc_now

# 索引管线的语义版本。**改动 payload 结构或索引含义时 bump**；
# 只改配置（模型名、截断长度）不用动它——那些会被下面的签名 hash 捕获。
# 两者的分工：这个号回答"格式是哪一代"，签名回答"这一代里用的哪套参数"。
INDEX_PIPELINE_VERSION = "1.0.0"

# payload 结构版本。加字段不 bump（旧 payload 读新字段得到默认值），
# 改已有字段的含义必须 bump——否则同一个字段在两代索引里指两个东西。
INDEX_SCHEMA = 1

# 存量索引的版本号。不带 hash：它的签名不可考，编一个就是伪造。
LEGACY_VERSION = "idx-legacy"

# 切分策略名。换 splitter 时同时改这里和 INDEX_PIPELINE_VERSION：
# 前者让签名变（自动生成新 collection），后者让"哪一代"这件事在库里可读。
SPLITTER = "structured-fragments/v1"

_CACHE_TTL_S = 2.0
_cache: dict[str, Any] = {"at": 0.0, "versions": [], "routing": []}


# ---------------------------------------------------------------------------
# 一、签名与命名
# ---------------------------------------------------------------------------

def current_signature() -> dict[str, Any]:
    """
    当前这套索引管线的签名。**只放真正会改变向量的东西。**

    为什么 embed_max_chars 要进签名：它决定送进模型的文本长度。同一段文字
    截 1500 和截 3000 得到两个不同的向量，检索结果会变，但它不在模型名里。
    这正是"改了配置以为没换版本"最容易漏的一项。

    为什么 pages_per_batch / batch_size 不进签名：它们只影响重建耗时与内存，
    不影响写进去的点。放进来会让"同一套索引"仅因为并发度不同就被判成两个版本。
    """
    from app.services.embedder import EmbedderService
    from app.services.ingest.limits import MAX_EMBED_CHARS

    return {
        "schema": INDEX_SCHEMA,
        "splitter": SPLITTER,
        "dense_model": EmbedderService.DENSE_MODEL_NAME,
        "sparse_model": EmbedderService.SPARSE_MODEL_NAME,
        "embed_max_chars": MAX_EMBED_CHARS,
    }


def fingerprint(sig: dict[str, Any]) -> str:
    raw = json.dumps(sig, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def version_of(sig: dict[str, Any] | None = None) -> str:
    """
    版本号 = `idx-<管线版本>-<签名前 8 位>`，例：`idx-1.0.0-3f7c1a90`。

    为什么把 hash 放进名字而不是只存库里：collection 名要能自解释。
    在 Qdrant 控制台里看到 `ai4s_papers__idx-1.0.0-3f7c1a90` 就能判断
    "这是第 1 代索引管线的第 3f7c1a90 套参数"，不用回查数据库。
    反过来只有 `ai4s_papers_v2` 这种名字，半年后没人说得清 v2 是什么。
    """
    sig = sig or current_signature()
    return f"idx-{INDEX_PIPELINE_VERSION}-{fingerprint(sig)[:8]}"


def collection_for(pipeline_version: str) -> str:
    """
    collection 命名：`<基础名>__<版本号>`，例 `ai4s_papers__idx-1.0.0-3f7c1a90`。

    用双下划线而不是点或短横：Qdrant 的 collection 名允许 `[A-Za-z0-9_-]`，
    而版本号里已经有一个短横（idx-1.0.0-…），再拿短横当分隔符就分不开了。
    legacy 版本是唯一例外——它的数据物理上就在基础名里。
    """
    if pipeline_version == LEGACY_VERSION:
        return settings.qdrant_collection
    return f"{settings.qdrant_collection}__{pipeline_version}"


# ---------------------------------------------------------------------------
# 二、版本登记表
# ---------------------------------------------------------------------------

def _rows(sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    with reader() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def _decode(row: dict[str, Any]) -> dict[str, Any]:
    row["signature"] = load_json(row.get("signature"), {})
    return row


def _invalidate() -> None:
    _cache["at"] = 0.0


def list_versions(include_retired: bool = True) -> list[dict[str, Any]]:
    """按创建时间倒序。默认带上已退役的——"上一版还在、可以回去"是这一层的卖点。"""
    now = time.time()
    if _cache["versions"] and now - _cache["at"] < _CACHE_TTL_S:
        rows = _cache["versions"]
    else:
        rows = [_decode(r) for r in _rows("SELECT * FROM index_versions ORDER BY created_at DESC")]
        _cache["versions"] = rows
        _cache["at"] = now
    if include_retired:
        return list(rows)
    return [r for r in rows if r["status"] != "retired"]


def get(pipeline_version: str) -> dict[str, Any] | None:
    for row in list_versions():
        if row["pipeline_version"] == pipeline_version:
            return row
    rows = _rows("SELECT * FROM index_versions WHERE pipeline_version = ?", (pipeline_version,))
    return _decode(rows[0]) if rows else None


def update_version(pipeline_version: str, **fields: Any) -> None:
    """只允许改已知列，避免拼 SQL 时把字段名拼进去。"""
    allowed = {
        "collection", "signature", "fingerprint", "status", "origin", "points",
        "chunk_count", "papers", "build_id", "note", "built_at", "activated_at", "retired_at",
    }
    cols = [(k, v) for k, v in fields.items() if k in allowed]
    if not cols:
        return
    sets = ", ".join(f"{k} = ?" for k, _ in cols)
    vals = [v if not isinstance(v, dict) else dump_json(v) for _, v in cols]
    with transaction() as c:
        c.execute(
            f"UPDATE index_versions SET {sets} WHERE pipeline_version = ?",
            (*vals, pipeline_version),
        )
    _invalidate()


def register(
    *,
    pipeline_version: str | None = None,
    signature: dict[str, Any] | None = None,
    collection: str | None = None,
    origin: str = "built",
    status: str = "building",
    note: str = "",
    build_id: str | None = None,
) -> dict[str, Any]:
    """
    登记一个版本（幂等）。已存在就原样返回，不覆盖——重建任务的进度
    与点数都挂在这一行上，重跑任务时把它清空等于把续跑断点丢了。
    """
    sig = signature if signature is not None else current_signature()
    pv = pipeline_version or version_of(sig)
    exist = get(pv)
    if exist:
        return exist

    coll = collection or collection_for(pv)
    now = utc_now()
    with transaction() as c:
        c.execute(
            """INSERT INTO index_versions
               (pipeline_version, collection, signature, fingerprint, status, origin,
                points, chunk_count, papers, build_id, note, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, ?)""",
            (
                pv, coll, dump_json(sig), fingerprint(sig) if sig else "",
                status, origin, build_id, note, now,
            ),
        )
    _invalidate()
    return get(pv) or {}


# ---------------------------------------------------------------------------
# 三、首次启动：存量登记与默认路由
# ---------------------------------------------------------------------------

def ensure_registry(client: Any | None = None) -> str:
    """
    保证"至少有一个版本 + 一条 global 路由"。返回当前写路径的版本号。

    存量库的处理是这一层的第一个真正的判断：`ai4s_papers` 里有向量、
    但没有签名。两个选择——(a) 按当前配置给它补一个签名，让它看起来像
    "用今天的参数建的"；(b) 明确登记成 legacy。
    选 (b)。(a) 会在下一次真的重建时给出一个 hash 相同的版本号，
    于是新索引被写进旧 collection 并覆盖掉旧点，而"重建期间查询不受影响"
    这条保证同时失效——这是这个功能最容易埋的雷。
    """
    if _rows("SELECT pipeline_version FROM index_versions LIMIT 1"):
        _ensure_global_row()
        return write_version()

    base = settings.qdrant_collection
    points = 0
    if client is not None:
        try:
            if client.collection_exists(base):
                points = client.count(collection_name=base, exact=False).count
        except Exception:
            points = 0

    if points:
        register(
            pipeline_version=LEGACY_VERSION,
            signature={},
            collection=base,
            origin="legacy",
            status="active",
            note="版本化之前的存量索引：签名不可考，不猜；可服务、可回滚，不可重建",
        )
        update_version(LEGACY_VERSION, points=points, activated_at=utc_now())
    else:
        sig = current_signature()
        register(signature=sig, origin="built", status="active", note="首次初始化")
        update_version(version_of(sig), activated_at=utc_now())

    _ensure_global_row()
    return write_version()


def _ensure_global_row() -> None:
    rows = _rows("SELECT * FROM index_routing WHERE scope = 'global'")
    if rows:
        return
    pv = _any_servable_version()
    if not pv:
        return
    with transaction() as c:
        c.execute(
            """INSERT INTO index_routing
               (id, scope, project_id, pipeline_version, stable_version, percent, enabled, note, updated_at)
               VALUES (?, 'global', '', ?, NULL, 0, 1, ?, ?)""",
            (new_id("rt"), pv, "初始化：全量走这一版", utc_now()),
        )
    _invalidate()


def _any_servable_version() -> str | None:
    for status in ("active", "ready"):
        rows = _rows(
            "SELECT pipeline_version FROM index_versions WHERE status = ? "
            "ORDER BY created_at LIMIT 1",
            (status,),
        )
        if rows:
            return rows[0]["pipeline_version"]
    rows = _rows("SELECT pipeline_version FROM index_versions ORDER BY created_at LIMIT 1")
    return rows[0]["pipeline_version"] if rows else None


# ---------------------------------------------------------------------------
# 四、切流：显式 > 项目 > 比例 > 默认
# ---------------------------------------------------------------------------

def _bucket(key: str) -> int:
    """
    稳定的分桶：同一个 key 永远落同一个桶。

    为什么不随机：灰度要能解释。用户问"为什么我这边是新版本、同事那边是旧版本"，
    答案是"你的课题被分到 12 号桶，灰度线是 30"；随机分桶只能回答"运气"。
    代价是"同一个课题反复问也只会落一侧"，所以同一问题的 A/B 要用
    `index_version` 显式指定，而不能指望灰度自己翻面。
    """
    return int(hashlib.sha1(key.encode("utf-8")).hexdigest()[:8], 16) % 100


def routing_rows() -> list[dict[str, Any]]:
    return _rows("SELECT * FROM index_routing ORDER BY scope, project_id")


def _routing_map() -> dict[tuple[str, str], dict[str, Any]]:
    return {(r["scope"], r["project_id"]): r for r in routing_rows()}


def write_version() -> str:
    """
    写路径用哪一版：**不跟灰度**。

    灰度期间如果新论文按比例写进新版、其余写进旧版，两边的语料就不一样了，
    于是"A/B 出来的差异"里混进了"旧版缺 12 篇论文"这个变量。
    所以写入永远落到灰度外的那一版（percent=0 时就是唯一那一版）。
    """
    g = _routing_map().get(("global", ""))
    if g:
        if g["percent"] > 0 and g.get("stable_version"):
            return g["stable_version"]
        return g["pipeline_version"]
    return _any_servable_version() or version_of()


def resolve(
    *,
    project_id: str | None = None,
    sample_key: str | None = None,
    explicit: str | None = None,
) -> dict[str, Any]:
    """
    解析这一次请求走哪一版索引。返回的结构直接进检索 trace。

    优先级：显式指定 > 项目规则 > 比例灰度 > 默认。
    顺序的理由：显式是"我在排查/做 A/B"，必须压过一切；项目规则是"这个课题
    已经验过了"，比抽样更硬；比例是最后才用的推广手段。
    """
    versions = {v["pipeline_version"]: v for v in list_versions()}
    routing = _routing_map()
    global_row = routing.get(("global", "")) or {}

    def pack(pv: str, rule: str, detail: str, **extra: Any) -> dict[str, Any]:
        row = versions.get(pv) or {}
        return {
            "pipeline_version": pv,
            "collection": row.get("collection") or collection_for(pv),
            "rule": rule,
            "detail": detail,
            "origin": row.get("origin") or "unknown",
            "status": row.get("status") or "unknown",
            "signature": row.get("signature") or {},
            "percent": 0,
            "bucket": None,
            "stable_version": None,
            "candidates": sorted(versions),
            **extra,
        }

    if explicit:
        if explicit not in versions:
            raise IndexVersionError(f"未知的 pipeline_version：{explicit}")
        return pack(explicit, "explicit", "调用方指定（评测/排查/强制 A/B）")

    if project_id and ("project", project_id) in routing:
        r = routing[("project", project_id)]
        note = (r.get("note") or "").strip()
        detail_text = f"课题 {project_id} 单独指定" + (f"：{note}" if note else "")
        return pack(r["pipeline_version"], "project", detail_text, percent=int(r["percent"]))

    g = global_row
    if not g:
        pv = _any_servable_version() or version_of()
        return pack(pv, "default", "尚无切流规则，走默认版本")

    percent = int(g["percent"] or 0)
    stable = g.get("stable_version") or g["pipeline_version"]
    if percent > 0 and percent < 100:
        key = project_id or sample_key or ""
        bucket = _bucket(key) if key else 0
        if key and bucket < percent:
            return pack(
                g["pipeline_version"], "percent",
                f"灰度 {percent}%：{key} 落在 {bucket} 号桶，命中新版",
                percent=percent, bucket=bucket, stable_version=stable,
            )
        return pack(
            stable, "percent",
            f"灰度 {percent}%：{key or '（无键）'} 落在 {bucket} 号桶，走稳定版",
            percent=percent, bucket=bucket, stable_version=stable,
        )

    return pack(g["pipeline_version"], "default", "全量走这一版")


# ---------------------------------------------------------------------------
# 五、切换与回滚
# ---------------------------------------------------------------------------

class IndexVersionError(Exception):
    """版本/切流参数不合法。API 层转 400，不吞成 500。"""


def set_global(version: str, *, percent: int = 0, stable: str | None = None, note: str = "") -> dict[str, Any]:
    """
    改 global 行。percent=0 是全量切换；0<percent<100 是灰度；100 也当全量。

    灰度必须显式给 stable（灰度外回落到哪版）。不给就取当前 global 行的
    pipeline_version——也就是"从当前在服务的这一版，放一部分流量到新版"，
    这是最常见的一次灰度，不该逼调用方填两遍。
    """
    if get(version) is None:
        raise IndexVersionError(f"未知的 pipeline_version：{version}")
    percent = max(0, min(100, int(percent)))
    cur = _routing_map().get(("global", "")) or {}

    if percent >= 100:
        # 100% 灰度就是全量。留一条 percent=100 的规则只会让人以为还在灰度，
        # 而实际行为与 percent=0 完全相同——两种表达同一个状态，必然有人读错。
        percent = 0
    if percent == 0:
        stable = None
    elif not stable:
        stable = cur.get("pipeline_version") or ""
        if stable == version:
            raise IndexVersionError("灰度目标与稳定版相同，等于没切")

    now = utc_now()
    with transaction() as c:
        if cur:
            c.execute(
                "UPDATE index_routing SET pipeline_version = ?, stable_version = ?, percent = ?,"
                " enabled = 1, note = ?, updated_at = ? WHERE id = ?",
                (version, stable, percent, note, now, cur["id"]),
            )
        else:
            c.execute(
                """INSERT INTO index_routing
                   (id, scope, project_id, pipeline_version, stable_version, percent, enabled, note, updated_at)
                   VALUES (?, 'global', '', ?, ?, ?, 1, ?, ?)""",
                (new_id("rt"), version, stable, percent, note, now),
            )
    _invalidate()
    target = get(version) or {}
    if percent == 0:
        # 全量切流后只能有一个 active。旧版降成 ready：它还在、还能服务、
        # 还能回滚，但"当前默认路径"只有一条。两个 active 并存时，
        # 任何按 status 找版本的代码都会拿到不确定的那一个。
        for row in list_versions():
            if row["pipeline_version"] != version and row["status"] == "active":
                update_version(row["pipeline_version"], status="ready")
        if target.get("status") == "ready":
            update_version(version, status="active", activated_at=now)
    # 灰度时不改状态：目标版是 ready（能服务、但不是默认），
    # 稳定版保持 active —— 这正是"灰度中"该有的状态形状。
    return {
        "pipeline_version": version,
        "collection": target.get("collection") or collection_for(version),
        "percent": percent,
        "stable_version": stable,
        "note": note,
    }


def set_project(project_id: str, version: str, *, percent: int = 100, note: str = "") -> dict[str, Any]:
    """
    给单个课题钉版本。percent 默认 100（课题级规则通常就是"这个课题用新版"），
    也允许 0<percent<100 做课题内抽样——但项目内抽样的意义不大，
    因为一个课题的查询往往是同一个人反复问同一批问题。
    """
    if get(version) is None:
        raise IndexVersionError(f"未知的 pipeline_version：{version}")
    if not project_id:
        raise IndexVersionError("project_id 不能为空")
    percent = max(0, min(100, int(percent)))
    now = utc_now()
    with transaction() as c:
        c.execute(
            """INSERT INTO index_routing
               (id, scope, project_id, pipeline_version, stable_version, percent, enabled, note, updated_at)
               VALUES (?, 'project', ?, ?, NULL, ?, 1, ?, ?)
               ON CONFLICT(scope, project_id) DO UPDATE SET
                 pipeline_version = excluded.pipeline_version,
                 percent = excluded.percent,
                 note = excluded.note,
                 updated_at = excluded.updated_at""",
            (new_id("rt"), project_id, version, percent, note, now),
        )
    _invalidate()
    return {"project_id": project_id, "pipeline_version": version, "percent": percent}


def clear_project(project_id: str) -> None:
    with transaction() as c:
        c.execute("DELETE FROM index_routing WHERE scope = 'project' AND project_id = ?", (project_id,))
    _invalidate()


def rollback(to: str | None = None) -> dict[str, Any]:
    """
    回滚 = 改一行指针。**不碰数据。**

    默认回到 global 行里的 stable_version（灰度中就是"退掉灰度"）；
    灰度已经撤掉（stable 为空）时，回到"除当前版本外最近一个 active/ready 的版本"。
    旧 collection 一直留着，所以回滚是毫秒级，不需要等重建。
    """
    cur = _routing_map().get(("global", "")) or {}
    target = to
    if not target:
        if cur.get("percent") and cur.get("stable_version"):
            target = cur["stable_version"]
        else:
            for v in list_versions():
                if v["pipeline_version"] != cur.get("pipeline_version") and v["status"] in ("active", "ready"):
                    target = v["pipeline_version"]
                    break
    if not target:
        raise IndexVersionError("没有可回滚的版本（除当前版本外没有 active/ready 的索引）")
    if get(target) is None:
        raise IndexVersionError(f"未知的 pipeline_version：{target}")
    return set_global(target, percent=0, note="回滚")


def detail(pipeline_version: str) -> dict[str, Any]:
    """单版本详情：登记信息 + 当前切流命中的课题。"""
    row = get(pipeline_version)
    if not row:
        raise IndexVersionError(f"未知的 pipeline_version：{pipeline_version}")
    hits = [r for r in routing_rows() if r["pipeline_version"] == pipeline_version]
    return {**row, "routing": hits}
