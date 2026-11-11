"""
三类不确定性状态的离线校验 + 触发用例。

两层：

**离线（纯函数，不联网、不调模型）**
1. `classify_chunk` 的四种「未识别」——用本语料里真实出现过的残渣文本当样本；
2. 符号取值抽取——两个真实反例（`β2 and β3 = 0.1`、`β2 of 10 m SMF`）不许抽出值；
3. 方向冲突——构造一对互斥结论 + 一对条件性分歧，验证两者被判成不同类型；
4. 空检索——构造一个"关键词一个都不在"的小语料，验证判空；
   再构造一个"关键词都在"的语料，验证**不**判空（宁可不报，不能误报）。

**在线（`--live`，打本地 HTTP）**
三类状态各一个触发用例，断言：
- `uncertainty.states` 里出现了对应键；
- `reject_reason` 不含「无法回答」/「抱歉」——那正是这个任务要拆掉的兜底句；
- 空检索的每条改写建议都带一个非空、且与原问题不同的 `query`（按钮点下去要有事发生）。

跑法：
    docker exec -i ai4s-api python /app/eval/uncertainty/check_uncertainty.py
    docker exec -i ai4s-api python /app/eval/uncertainty/check_uncertainty.py --live
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request

sys.path.insert(0, "/app")

from app.services.rag import uncertainty as U  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------------------
# 1. 未识别：四种形态
# ---------------------------------------------------------------------------
def test_classify() -> None:
    print("\n=== 1. classify_chunk：四种「未识别」")
    cases = [
        # 本语料实测样本：ViT p7 的表格只抽到 2 个非空单元格；Attention p13 只剩一根竖线
        ("表格空壳（竖线）", "|  |  |  |  |  |  |", "table", None, "table_empty"),
        ("表格空壳（单竖线）", "|", "table", None, "table_empty"),
        ("表格错切（BERT p3）", "BER | T B | ERT", "table", None, "table_empty"),
        ("正常表格", "Model | Layers | Hidden\nBERT-base | 12 | 768", "table", None, None),
        # figure：只有图注 vs 模板回填文案
        ("只有图注", "FIGURE 5 (A-H) Time-domain evolution of soliton molecules", "figure", None, "figure_caption_only"),
        ("模板回填图注", "该图表展示了不同实验组在关键性能指标上的对比结果。", "figure", None, "figure_synthetic"),
        ("扫描页", "some ocr text here", "text", {"text_layer": False}, "scan_page"),
        ("OCR 精度", "some ocr text here", "text", {"precision": "ocr"}, "scan_page"),
        ("正常正文", "We use the Adam optimizer with beta2 = 0.98.", "text", {"text_layer": True}, None),
    ]
    for name, text, kind, anchors, want in cases:
        got = U.classify_chunk(text, kind, anchors)
        check(name, got == want, f"got={got} want={want}")


# ---------------------------------------------------------------------------
# 2. 符号取值抽取：两个真实反例
# ---------------------------------------------------------------------------
def test_values() -> None:
    print("\n=== 2. _values_after：不许把别的东西当成这个符号的值")
    pat = r"β\s*[_^]?\s*\{?2\}?"
    # 真实文本①：β2 的值被省略（两个符号共用一个 =），不能给 β2 抽到 3（那是 β3 的下标）
    t1 = "γ = 3/W/km, β2 and β3 = 0.1 ps2/nm/km are Kerr nonlinear"
    v1 = U._values_after(t1, pat)
    check("`β2 and β3 = 0.1` 不给 β2 抽值", v1 == [], f"got={v1}")

    # 真实文本②：`β2 of 10 m SMF` 里的 10 m 是光纤**长度**，不是 β2 的值
    t2 = "β2 of 10 m SMF and 15 m gain fiber are −20 and −30ps/nm/km, respectively"
    v2 = U._values_after(t2, pat)
    check("`β2 of 10 m SMF` 不抽成 10 m", v2 == [], f"got={v2}")

    # 正例：三种写法都要抽得出来
    t3 = "with β1 = 0.9, β2 = 0.999, L2 weight decay of 0.01"
    v3 = U._values_after(t3, pat)
    check("`β2 = 0.999` 抽到 0.999", len(v3) == 1 and v3[0]["norm"] == "0.999", f"got={v3}")

    t4 = "β2 = −20 ps/nm/km"
    v4 = U._values_after(t4, pat)
    check("负值与单位", len(v4) == 1 and v4[0]["norm"] == "-20" and "ps" in v4[0]["unit"], f"got={v4}")

    # 0.980 与 0.98 是同一个值，不能算成两处冲突
    check("0.980 与 0.98 归一后相同", U._norm_value("0.980") == U._norm_value("0.98"))


# ---------------------------------------------------------------------------
# 3. 方向冲突
# ---------------------------------------------------------------------------
def _claim(text: str, arxiv: str, page: int = 1) -> dict:
    return {
        "claim_id": text[:6],
        "text": text,
        "citations": [
            {"arxiv_id": arxiv, "chunk_id": f"c_{arxiv}", "title": arxiv, "page": page, "text": text}
        ],
    }


def test_polarity() -> None:
    print("\n=== 3. 方向冲突：互斥 vs 条件性分歧")
    # 条件性：两边都带条件（"在小数据下" / "在大数据下"），都是对的
    cond = [
        _claim("在小数据下 Transformer 不如 ResNet。", "2010.11929"),
        _claim("在大数据下 Transformer 优于 ResNet。", "1706.03762"),
    ]
    c1 = U._polarity_conflicts(cond)
    check("条件不同 → conditional_split", len(c1) == 1 and c1[0]["type"] == U.TYPE_CONDITIONAL,
          f"got={[x['type'] for x in c1]}")

    # 互斥：两边都没有条件限定，且方向相反
    direct = [
        _claim("LoRA 增加了推理延迟。", "2106.09685"),
        _claim("LoRA 降低了推理延迟。", "1706.03762"),
    ]
    c2 = U._polarity_conflicts(direct)
    check("无条件且反向 → polarity", len(c2) == 1 and c2[0]["type"] == U.TYPE_POLARITY,
          f"got={[x['type'] for x in c2]}")

    # 同向不算冲突（两句都在说"提升"）
    same = [
        _claim("BERT 提升了 GLUE 上的表现。", "1810.04805"),
        _claim("ViT 提升了 ImageNet 的准确率。", "2010.11929"),
    ]
    check("同向不报冲突", U._polarity_conflicts(same) == [])

    # 对象没有交集不算冲突（两件事）
    diff = [
        _claim("BERT 提升了准确率。", "1810.04805"),
        _claim("LoRA 降低了显存占用。", "2106.09685"),
    ]
    check("对象无交集不报冲突", U._polarity_conflicts(diff) == [])


# ---------------------------------------------------------------------------
# 4. 空检索
# ---------------------------------------------------------------------------
def _corpus(chunks: list[tuple[str, str, str]]) -> dict:
    """chunks: (arxiv_id, title, text)"""
    papers = {}
    for aid, title, _ in chunks:
        papers[aid] = {"arxiv_id": aid, "title": title, "chunks": 1}
    return {
        "chunks": [
            {"id": f"c{i}", "payload": {"arxiv_id": aid, "title": title, "text": text,
                                        "kind": "paragraph", "page": i + 1}}
            for i, (aid, title, text) in enumerate(chunks)
        ],
        "papers": papers,
    }


class _Src:
    """假的 RetrievalResult：只需要 uncertainty 用到的几个属性。"""

    def __init__(self, cid: str, aid: str, text: str, page: int = 1, score: float = 0.3):
        self.chunk_id, self.arxiv_id, self.title = cid, aid, aid
        self.text, self.page, self.kind, self.score = text, page, "paragraph", score


def test_empty() -> None:
    print("\n=== 4. 空检索：判空 / 不判空")
    corpus = _corpus(
        [
            ("1706.03762", "Attention Is All You Need", "We use the Adam optimizer with beta2 = 0.98."),
            ("1810.04805", "BERT", "We use Adam with learning rate of 1e-4 and warmup steps."),
        ]
    )
    srcs = [_Src("c0", "1706.03762", "We use the Adam optimizer.")]

    e1 = U.empty_state(
        question="蛋白质折叠的自由能垒是多少？",
        search_text="What is the free energy barrier for protein folding?",
        plan_type="point", sources=srcs, retrieval_weak=True,
        top_raw=0.01, threshold=0.015, corpus=corpus,
    )
    check("关键词不在库内 → 判空", bool(e1) and e1["reason"] == "no_lexical_hit",
          f"got={e1 and e1['reason']}")
    check("判空时 blocking=True（跳过生成）", bool(e1) and e1["blocking"] is True)
    check("空态带改写建议", bool(e1) and len(e1["suggestions"]) >= 1)
    for s in (e1 or {}).get("suggestions") or []:
        check(f"建议「{s['id']}」可点击（query 非空且不同于原问）",
              bool(s["query"]) and s["query"] != "蛋白质折叠的自由能垒是多少？", s["query"])
    check("空态带检索范围", bool(e1) and e1["scope"]["chunks"] == 2, f"chunks={(e1 or {}).get('scope', {}).get('chunks')}")
    check("空态带最近邻（说明系统看到了什么）", bool(e1) and len(e1["nearest"]) == 1)

    # 关键词都在：不许判空。这是"宁可漏报也不能误报"的那一半。
    e2 = U.empty_state(
        question="Adam 的 beta2 是多少？",
        search_text="What is beta2 in the Adam optimizer?",
        plan_type="point", sources=srcs, retrieval_weak=False,
        top_raw=0.4, threshold=0.015, corpus=corpus,
    )
    check("关键词命中 → 不判空", e2 is None, f"got={e2 and e2['reason']}")

    # 库是空的
    e3 = U.empty_state(
        question="随便问点什么？", search_text="anything at all",
        plan_type="generic", sources=[], retrieval_weak=False,
        top_raw=None, threshold=0.0, corpus=_corpus([]),
    )
    check("空库 → no_corpus", bool(e3) and e3["reason"] == "no_corpus", f"got={e3 and e3['reason']}")

    # 语料快照取不到（Qdrant 抖动）：不能替自己不知道的事实下结论
    e4 = U.empty_state(
        question="随便问点什么？", search_text="anything at all",
        plan_type="generic", sources=[], retrieval_weak=False,
        top_raw=None, threshold=0.0, corpus=None,
    )
    check("语料不可用 → no_hits 而不是 no_corpus", bool(e4) and e4["reason"] == "no_hits",
          f"got={e4 and e4['reason']}")

    # 兜底文案里不许出现"无法回答"
    for e in (e1, e3, e4):
        if not e:
            continue
        text = U.reason_text(e)
        check(f"reject_reason 不含「无法回答」（{e['reason']}）",
              "无法回答" not in text and "抱歉" not in text, text[:60])


# ---------------------------------------------------------------------------
# 4b. 规则文案：占位符必须被填掉
# ---------------------------------------------------------------------------
def test_rules() -> None:
    """
    规则区是给用户核对判据用的。里面留着一个 {papers}、或者写死一个 N，
    整块就从"解释"变成"模板"——界面上真的显示成字面 N 过一次（截图 09）。
    """
    with_papers = U._rules({"papers": {"a": {}, "b": {}, "c": {}, "d": {}, "e": {}}})
    empty_rule = next(r for r in with_papers if r["state"] == "empty")
    check("规则里的篇数被填成真实数字", "这 5 篇" in empty_rule["next"], empty_rule["next"])
    check("规则里没有残留占位符",
          all("{" not in r["next"] and " N " not in r["next"] for r in with_papers))

    without = U._rules(None)
    e2 = next(r for r in without if r["state"] == "empty")
    check("取不到语料时不编数字", "这 " not in e2["next"] and "{" not in e2["next"], e2["next"])

    payload = U.none_payload()
    check("闲聊路径也带规则（前端同一套分支）", len(payload["rules"]) == 3)
    check("闲聊路径的规则已填好", all("{" not in r["next"] for r in payload["rules"]))


# ---------------------------------------------------------------------------
# 5. 在线触发（三类状态各一例）
# ---------------------------------------------------------------------------
def ask(question: str, base: str = "http://127.0.0.1:8000") -> dict:
    req = urllib.request.Request(
        f"{base}/api/v1/qa/ask",
        data=json.dumps({"question": question, "top_k": 6}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def test_live(base: str) -> None:
    print("\n=== 5. 在线触发用例")

    # --- 空检索 ---
    print("\n  [空检索] 蛋白质折叠的自由能垒是多少？")
    d = ask("蛋白质折叠的自由能垒是多少？", base)
    u = d.get("uncertainty") or {}
    e = u.get("empty") or {}
    check("states 含 empty", "empty" in (u.get("states") or []), str(u.get("states")))
    check("reason = no_lexical_hit", e.get("reason") == "no_lexical_hit", str(e.get("reason")))
    check("rejected=True 且不写「无法回答」",
          d.get("rejected") is True and "无法回答" not in (d.get("reject_reason") or ""),
          d.get("reject_reason") or "")
    check("有检索范围", (e.get("scope") or {}).get("papers", 0) > 0,
          f"papers={(e.get('scope') or {}).get('papers')}")
    check("有关键词命中表", len((e.get("query") or {}).get("terms") or []) > 0)
    check("至少 2 条改写建议（一条备用）", len(e.get("suggestions") or []) >= 2,
          str([s["kind"] for s in e.get("suggestions") or []]))

    # 用建议真的能问出一个正常答案——这是"下一步动作"是否成立的验收
    sug = (e.get("suggestions") or [None])[0]
    if sug:
        d2 = ask(sug["query"], base)
        check(f"点建议「{sug['label']}」能正常作答",
              bool(d2.get("answer")) and not d2.get("rejected"), (d2.get("answer") or "")[:50])

    # --- 证据冲突 ---
    print("\n  [证据冲突] β2 设为多少")
    d = ask("β2 设为多少", base)
    u = d.get("uncertainty") or {}
    cf = u.get("conflicts") or []
    check("states 含 conflict", "conflict" in (u.get("states") or []), str(u.get("states")))
    check("至少一处冲突", len(cf) >= 1, f"n={len(cf)}")
    if cf:
        c = cf[0]
        check("冲突有两边", len(c["sides"]) >= 2, f"n={len(c['sides'])}")
        check("两边都有出处页码",
              all(any(p.get("page") for p in s["papers"]) for s in c["sides"]))
        check("明写不下判断", "不判断" in (c.get("not_judged") or ""), c.get("not_judged", "")[:40])
        check("dimension 可读", bool(c.get("dimension")), c.get("dimension", ""))
        vals = sorted(s["value"] for s in c["sides"])
        print(f"      两侧取值：{vals}")

    # --- 表格/图表未识别 ---
    print("\n  [表格未识别] ViT 在 ImageNet 上的准确率是多少")
    d = ask("ViT 在 ImageNet 上的准确率是多少", base)
    u = d.get("uncertainty") or {}
    un = u.get("unreadable") or []
    check("states 含 unreadable", "unreadable" in (u.get("states") or []), str(u.get("states")))
    check("至少一处未识别", len(un) >= 1, f"n={len(un)}")
    if un:
        x = un[0]
        check("带页码", x["page"] > 0, f"p{x['page']}")
        check("带 kind 与可读文案", f"unreadable.{x['kind']}" in U.COPY and bool(x["title"]),
              f"{x['kind']} / {x['title']}")
        check("有「查看原页」动作", (x.get("action") or {}).get("type") == "open_page")
        said = f"{x['title']}{x['detail']}"
        check("文案说清是「结构未复原 / 扫描件 / 只有图注」哪一种",
              any(w in said for w in ("未复原", "扫描件", "图注")), said[:50])

    print("\n  [图表未识别] 孤子分子的时域演化是什么样的")
    d = ask("孤子分子的时域演化是什么样的", base)
    u = d.get("uncertainty") or {}
    un = u.get("unreadable") or []
    check("figure 只读到图注 → figure_caption_only",
          any(x["kind"] == "figure_caption_only" for x in un),
          str([x["kind"] for x in un]))
    check("被引用的那条排在最前（severity 高）",
          not un or un[0].get("cited") is True,
          str([(x["kind"], x["cited"]) for x in un]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="额外打本地 API 跑三类触发用例")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    args = ap.parse_args()

    test_classify()
    test_values()
    test_polarity()
    test_empty()
    test_rules()
    if args.live:
        test_live(args.base)

    print("\n" + "=" * 56)
    if FAILS:
        print(f"FAILED {len(FAILS)}:")
        for f in FAILS:
            print("  -", f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
