"""
写作溯源探针：跑一次真实生成，把 md + json 落盘，并在终端打印"可核对的那几件事"。

为什么要有这个脚本（而不是直接点界面）：
界面上能看到的只有渲染结果。答辩/复盘要回答的是"这次到底校验掉了什么"——
无引用句删了几条、数字不落地删了几条、有没有库外文献被清掉、占位几段。
这些只在 `stats.dropped_by_reason` 与 `trace` 里，脚本把它们打出来。

用法：
    docker exec ai4s-api python scripts/writing_probe.py \
        --topic "参数高效微调的证据链" \
        --out /app/eval/writing_sample.md --json /app/eval/writing_sample.json
"""
from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(0, "/app")

from app.services.writing import WritingPipeline  # noqa: E402


def selftest() -> int:
    """
    三道闸的确定性自检。

    为什么要单独做一遍：真实生成里"模型恰好没编数字、没编文献"是完全可能的，
    于是校验器看起来像没起作用。这三条用固定输入跑，能证明闸是真的在那儿——
    它拦不拦，取决于输入，不取决于运气。
    """
    from app.services.writing.references import audit_text
    from app.services.writing.sentences import (
        numbers_grounded,
        parse_units,
        sentence_type,
    )

    print("=" * 72)
    print("闸门自检（固定输入，不调模型）")

    # ① bridge 伪装
    body = "LoRA 把可训练参数减少了 10,000 倍。"
    stype, why = sentence_type(body, "bridge")
    print("\n① bridge 伪装成过渡句")
    print(f"   模型声明：bridge   实际判定：{stype}（{why}）")
    print(f"   无引用 → {'整句删除' if stype == 'claim' else '保留'}")

    # ② 数字不落地
    evidence = "LoRA reduces the number of trainable parameters by 10,000 times."
    ok, missing = numbers_grounded("LoRA 把可训练参数减少了 10,000 倍，显存降低 3.5 倍。", evidence)
    print("\n② 数字编造")
    print("   证据原文只提到 10,000   正文写了 10,000 与 3.5")
    print(f"   落地：{ok}   找不到的数字：{missing} → 整句删除")

    # ③ 库外文献
    refs = [{"arxiv_id": "2106.09685", "doi": "", "ref_no": 1}]
    out = audit_text("已有工作（arXiv:2401.12345，doi:10.9999/fake.2024）给出了相同结论。", refs)
    print("\n③ 库外文献标识")
    print(f"   清理后正文：{out['text']}")
    print(f"   被清掉的标识：{out['fabricated']}")

    # ④ 方括号假引用
    units = parse_units("该方法在多个任务上有效[12]。")
    print("\n④ 论文式方括号引用")
    print(f"   原文：该方法在多个任务上有效[12]。")
    print(f"   清理后：{units[0]['text']}   引用编号：{units[0]['cites']}（空 = 这条假引用没被当成真引用）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", default="")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--sections", type=int, default=None)
    ap.add_argument("--arxiv-ids", default="", help="逗号分隔，限定文献范围")
    ap.add_argument("--out", default="")
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.topic:
        ap.error("--topic 必填（或用 --selftest）")

    ids = [x.strip() for x in args.arxiv_ids.split(",") if x.strip()] or None
    result = WritingPipeline().generate(args.topic, n_sections=args.sections, arxiv_ids=ids)
    stats = result.get("stats") or {}

    print("=" * 72)
    print(f"主题：{result['topic']}")
    print(f"口气：{stats.get('prompt_version')} / {stats.get('config_version')}/{stats.get('fingerprint')}")
    print("\n## 段落 / 句子")
    for sec in result["sections"]:
        head = f"[{sec['no']}] {sec['title']}"
        if sec.get("placeholder"):
            print(f"{head}  —— 占位：{sec.get('gap')}")
            continue
        print(f"{head}  证据 {len(sec.get('evidence') or [])} 段")
        for s in sec["sentences"]:
            marks = "".join(f"[引#{n}]" for n in s["cite_nos"])
            tag = "claim " if s["type"] == "claim" else "bridge"
            print(f"    ({tag}) {s['text']}{marks}")
            if s["type"] == "bridge":
                print("             ↑ 无需引用")
            elif s.get("chunk_ids"):
                print(f"             chunk_id={s['chunk_ids'][0]}  ref={s.get('ref_nos')}")

    print("\n## 参考文献（全部来自库内）")
    for ref in result["references"]:
        from app.services.writing.references import format_reference

        print("  " + format_reference(ref))
        print(f"      chunk: {ref['chunk_nos']}  checked={ref['checked']} issues={ref['issues']}")

    print("\n## 计数")
    for key in (
        "sections",
        "sentences",
        "claim_sentences",
        "bridge_sentences",
        "cited_sentences",
        "uncited_claim",
        "placeholders",
        "sentences_dropped",
        "refs",
        "refs_rejected",
        "fabricated_refs",
        "evidence_chunks",
        "papers",
        "degraded",
    ):
        print(f"  {key:<18}{stats.get(key)}")
    if stats.get("dropped_by_reason"):
        print(f"  删句原因          {json.dumps(stats['dropped_by_reason'], ensure_ascii=False)}")
    timing = (result.get("trace") or {}).get("timing") or {}
    print(f"  耗时              {json.dumps(timing, ensure_ascii=False)}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(result["markdown"])
        print(f"\nmarkdown → {args.out}")
    if result.get("rejected"):
        print(f"\n拒答：{result.get('reject_reason')}")
        return 1
    if args.json:
        payload = {
            "topic": result["topic"],
            "config": result.get("config"),
            "sections": result["sections"],
            "references": result["references"],
            "ref_map": result["ref_map"],
            "stats": stats,
            "trace": result.get("trace"),
        }
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"json     → {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
