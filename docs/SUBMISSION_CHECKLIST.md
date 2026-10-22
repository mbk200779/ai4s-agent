# 大赛作品提交材料清单

> 根据官方提交要求整理。已就绪的打 ✅，仍需你补充/操作的打 ⬜。

## 一、代码与仓库材料

| 要求项 | 状态 | 当前位置 / 说明 | 待办 |
|---|---|---|---|
| 托管平台（GitHub / Gitee Public 或评审可访问） | ⬜ | 已提供一键脚本，需在本地执行 | 运行 `scripts/git-setup.sh` 或 `scripts/git-setup.ps1`，详见 `docs/2.1-托管与提交准备.md` |
| 代码结构清晰、含有效注释、遵循主流规范 | ✅ | `backend/app/`、`frontend/src/`、各 `__init__.py` 与模块文档字符串 | 已按模块拆分，含中文注释与关键决策说明 |
| README 文件（项目介绍、技术架构、安装运行指南、团队信息） | ✅ | `README.md` | 团队信息处已留占位符，需填入真实队名/成员/联系方式/仓库地址 |
| 完整的 Git 提交历史 | ⬜ | 未初始化仓库 | 建议分阶段提交（如 phase-0 ~ phase-10），体现真实开发过程 |
| 环境配置（环境变量模板 + 依赖清单） | ✅ | `.env.example`、`backend/requirements.txt`、`frontend/package.json`、`docker-compose.yml`、`Makefile` | API key 通过 `.env` 注入，未出现在代码中 |

## 二、演示视频

| 要求项 | 状态 | 说明 | 待办 |
|---|---|---|---|
| 时长 5–8 分钟 | ⬜ | 需自行录制 | 按 `docs/DEMO_SCRIPT.md` 脚本录制 |
| 内容：项目介绍 + 核心功能演示 + AI 技术亮点 | ⬜ | 脚本已覆盖上传 → 解析 → 问答溯源 → 图谱 → 综述 → 绘图 → 评测 diff=0 | 配合本地运行环境录制 |
| MP4 格式，分辨率 ≥1080p | ⬜ | 需自行导出 | 使用 OBS / 剪映等工具录制并导出 |
| 普通话讲解，可配字幕 | ⬜ | 建议配字幕提升评审体验 | 剪映可自动生成字幕 |

**推荐视频结构（直接复用 DEMO_SCRIPT.md）：**

1. 0:00-0:30 开场定位：可验证、可复现、可溯源。
2. 0:30-1:30 上传/arXiv 导入 + 结构化解析。
3. 1:30-3:00 可溯源问答 + 点击引用回跳 PDF 原位高亮。
4. 3:00-4:00 引用图谱缩放/平移/点跳。
5. 4:00-5:00 综述生成 + Markdown 导出。
6. 5:00-6:00 绘图辅助（matplotlib/mermaid 代码生成）。
7. 6:00-7:30 `make eval` 一键评测 + 两次运行 diff=0。
8. 7:30-8:00 总结三条主线。

## 三、技术文档（PDF，≤30 页）

| 要求项 | 状态 | 当前素材 | 待办 |
|---|---|---|---|
| PDF 格式，不超过 30 页 | ⬜ | 已提供完整 Markdown：`docs/技术文档.md` | 用 Typora / VSCode Markdown PDF / Pandoc 导出为 PDF |
| 项目概述、需求分析、系统架构设计 | ✅ | `docs/技术文档.md` 第 1–3 章 | 直接复用 |
| AI 技术方案、核心功能实现 | ✅ | `docs/技术文档.md` 第 4–5 章 | 直接复用 |
| 测试与部署 | ✅ | `docs/技术文档.md` 第 6 章 | 直接复用 |
| 总结与展望 | ✅ | `docs/技术文档.md` 第 8 章 | 已包含 |
| AI 技术选型理由、集成方式、优化策略 | ✅ | `docs/技术文档.md` 第 4 章“为什么这样选” | 直接复用 |
| 技术创新点与应用创新点 | ✅ | `docs/技术文档.md` 第 7 章 | 直接复用 |

**建议 PDF 目录：**

1. 项目概述
2. 需求分析
3. 系统架构设计（含 mermaid 架构图）
4. AI 技术方案与选型理由
5. 核心功能实现（PDF 解析 / 检索 / QA / 推理 / 图谱 / 综述 / 绘图 / 前端）
6. 测试与部署（`make eval`、diff=0 证明）
7. 技术创新点与应用创新点
8. 总结与展望

> 可将 `docs/assets/generate_defense_image.png` 放在“测试与部署”章节作为一页图，一眼说明评测报告 + 溯源高亮。

## 四、在线演示

| 要求项 | 状态 | 说明 | 待办 |
|---|---|---|---|
| 可公网访问的在线演示地址 | ⬜ | 当前仅在本地 Docker 运行 | 需部署到云服务器或 WorkBuddy Sites / Cloud Service |
| 评审期间保持服务可用 | ⬜ | 需持续运行 | 部署后保持容器运行 |
| 如需登录，提供评审专用测试账号 | ✅ | `.env.example` 中已配置 `JUDGE_USERNAME=judge`、`JUDGE_PASSWORD=ai4s2024` | 在线部署后使用该账号 |

**部署建议（三选一）：**

1. **自有云服务器**：安装 Docker + Docker Compose，`git clone` 后 `make up`，开放 3000/8000 端口。
2. **WorkBuddy Cloud Service**：使用 `cloud-service` skill 一键接入云端数据库/对象存储/LLM（需根据比赛规则判断是否允许）。
3. **WorkBuddy Sites**：仅适合静态前端；本项目为全栈服务，建议方案 1 或 2。

## 五、提交前自查

- [ ] `.env` / API key 未提交到公开仓库（`.gitignore` 已包含 `.env`）。
- [ ] `README.md` 团队信息已填写真实内容。
- [ ] 代码已 push 到 GitHub / Gitee 且仓库为 Public（或已给评审账号权限）。
- [ ] 演示视频已导出为 MP4，1080p，时长 5–8 分钟。
- [ ] 技术文档已导出为 PDF，≤30 页。
- [ ] 在线演示地址可访问，且评审账号可用。
- [ ] 所有材料已按大赛官网要求上传。

## 六、当前已就绪文件速查

```text
.
├── README.md                          # 项目介绍、架构、运行指南、团队信息占位
├── .env.example                       # 环境变量模板（无敏感信息）
├── docker-compose.yml                 # 6 服务编排
├── Makefile                           # up/down/logs/eval/help
├── backend/
│   ├── requirements.txt               # Python 依赖
│   ├── Dockerfile
│   ├── app/                           # FastAPI 业务代码
│   ├── scripts/ingest.py              # 批量入库
│   ├── scripts/eval_runner.py         # 一键评测
│   └── eval/golden_qa.json            # 40 条 golden QA
├── frontend/
│   ├── package.json                   # Node 依赖
│   ├── Dockerfile
│   └── src/app/                       # Next.js 页面
└── docs/
    ├── ARCHITECTURE.md                # 模块架构 + Prompt 版本
    ├── TECH_DOC.md                    # 技术文档（含并排图）
    ├── DEMO_SCRIPT.md                 # 5–8 分钟答辩脚本
    ├── SUBMISSION_CHECKLIST.md        # 本文件
    └── assets/generate_defense_image.png   # 评测报告 + 溯源截图并排图
```
