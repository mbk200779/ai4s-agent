#!/usr/bin/env python
"""
生成「提交材料清单.md」：按 `git ls-files` 逐项列出工作树文件与体积。

为什么用脚本而不是手写：清单的价值在于「和仓库逐项对得上」。手写一定会漂移——
新增一个文件忘了加，或者文件数还是旧的。这里让清单直接从索引生成，每次改动后重跑即可。

用法：
    python scripts/gen_submission_manifest.py                  # 输出到当前目录的 提交材料清单.md
    python scripts/gen_submission_manifest.py <输出路径.md>

注意：`git ls-files` 默认 `core.quotepath=true`，中文路径会被转成八进制转义，
所以这里显式带 `-c core.quotepath=false`。
"""
from __future__ import annotations

import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_URL = "https://github.com/mbk200779/ai4s-agent"

# 分组顺序 = 清单里的呈现顺序。匹配用「前缀 + 层级」，不用通配，避免误伤。
GROUPS: list[tuple[str, object]] = [
    ("根目录文件（提交必需）", lambda p: "/" not in p),
    (".vscode 编辑器配置", lambda p: p.startswith(".vscode/")),
    ("backend 根", lambda p: p.startswith("backend/") and p.count("/") == 1),
    ("backend/app", lambda p: p.startswith("backend/app/")),
    ("backend/eval", lambda p: p.startswith("backend/eval/")),
    ("backend/scripts", lambda p: p.startswith("backend/scripts/")),
    ("deploy 部署与运维脚本", lambda p: p.startswith("deploy/")),
    ("docs 根（文档正文）", lambda p: p.startswith("docs/") and p.count("/") == 1),
    ("docs/assets", lambda p: p.startswith("docs/assets/")),
    ("frontend 根", lambda p: p.startswith("frontend/") and p.count("/") == 1),
    ("frontend/public", lambda p: p.startswith("frontend/public/")),
    ("frontend/src", lambda p: p.startswith("frontend/src/")),
    ("output 文档流水线产物", lambda p: p.startswith("output/")),
    ("scripts 构建脚本", lambda p: p.startswith("scripts/")),
]


def git(*args: str) -> str:
    return subprocess.check_output(["git", "-c", "core.quotepath=false", *args], cwd=REPO, text=True, encoding="utf-8")


def human(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{max(1, round(n / 1024))} KB"
    return f"{n / 1048576:.1f} MB"


def main() -> int:
    files = [p for p in git("ls-files").splitlines() if p.strip()]
    head = git("log", "-1", "--format=%h").strip()
    out_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO, "提交材料清单.md")

    buckets: dict[str, list[str]] = {}
    for p in files:
        for title, pred in GROUPS:
            if pred(p):  # type: ignore[operator]
                buckets.setdefault(title, []).append(p)
                break
        else:
            print(f"[warn] 未分组，已跳过：{p}", file=sys.stderr)

    total = sum(os.path.getsize(os.path.join(REPO, p)) for p in files)
    lines: list[str] = [
        "# 传智杯参赛作品 —— 提交材料清单",
        "",
        "> 本文件由 `scripts/gen_submission_manifest.py` 按 `git ls-files` 逐项生成，与仓库跟踪文件一一对应。",
        f"> 仓库：`{REPO_URL}`（HEAD `{head}` / {len(files)} 文件）",
        f"> 生成位置：`{out_path}`",
        "",
        "## 一、总览",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 文件总数 | {len(files)} |",
        f"| 总体积 | {total / 1048576:.1f} MB |",
        "| 目录结构 | 与仓库完全一致，未重排、未改名 |",
        "",
        "## 二、对应官方 2.1–2.5",
        "",
        "| 官方条目 | 对应材料 | 在本包中的位置 |",
        "|---|---|---|",
        "| 2.1 托管与代码 | 全部源码 + README + 环境配置 + Git 记录 | `backend/`、`frontend/`、`deploy/`、`scripts/`、`README.md`、`.env.example`、`docker-compose.yml`；Git 历史见 GitHub 仓库（未复制 `.git`） |",
        "| 2.2 演示视频 | 录制逐字稿（视频本体录完放 `演示视频/`） | `演示视频录制脚本.md`、`docs/DEMO_SCRIPT.md` |",
        "| 2.3 技术文档 | PDF + Markdown 源 | `docs/AI4S学术研究Agent-技术文档.pdf`、`docs/技术文档.md`、`docs/TECH_DOC.md` |",
        "| 2.4 在线演示 | 部署配置、隧道、自检与保活脚本 | `deploy/`、`docker-compose.yml`、`docs/2.24`、`docs/2.25` |",
        "| 2.5 提交注意 | 清单与密钥自查 | `docs/SUBMISSION_CHECKLIST.md` |",
        "",
        "## 三、逐项文件清单",
        "",
    ]

    n = 0
    for title, _ in GROUPS:
        items = sorted(buckets.get(title, []))
        if not items:
            continue
        lines += [f"### {title}", "", f"共 {len(items)} 个文件", "", "| # | 路径 | 大小 |", "|---|---|---|"]
        for p in items:
            n += 1
            lines.append(f"| {n} | `{p}` | {human(os.path.getsize(os.path.join(REPO, p)))} |")
        lines.append("")

    lines += [
        "## 四、未包含的内容及原因",
        "",
        "| 内容 | 原因 |",
        "|---|---|",
        "| `.git/` 目录 | Git 历史由 GitHub 仓库承载；提交包只需工作树文件 |",
        "| `.env` | 含真实密钥，已在 `.gitignore` 中，绝不可提交 |",
        "| `node_modules/`、`.next/`、`__pycache__/`、`model_cache/`、`minio_data/`、`redis_data/` | 构建产物与运行时数据，可由 `docker compose up` 重建 |",
        "| 演示视频 MP4 | 由 `演示视频录制脚本.md` 录出后放入 `演示视频/`，不进代码仓库 |",
        "",
        "## 五、复现这份清单",
        "",
        "```bash",
        "python scripts/gen_submission_manifest.py            # 输出到仓库根",
        "python scripts/gen_submission_manifest.py ~/清单.md   # 输出到指定位置",
        "```",
        "",
        "清单由 `git ls-files` 直接生成，改完代码重跑一次即可，不存在「文档和实际文件对不上」。",
        "",
    ]

    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")

    print(f"已生成：{out_path}（{len(files)} 个文件，{total / 1048576:.1f} MB，HEAD {head}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
