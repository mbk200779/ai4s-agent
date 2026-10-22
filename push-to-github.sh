#!/bin/bash
# 一键把当前代码推送到 GitHub 仓库：https://github.com/mbk200779/ai4s-agent.git
# 运行前请确保：1. 已安装 git；2. 已创建 GitHub 空仓库；3. 知道 GitHub Personal Access Token（PAT）

set -e

REPO_URL="https://github.com/mbk200779/ai4s-agent.git"

echo ""
echo "=========================================="
echo "  正在推送代码到 GitHub：mbk200779/ai4s-agent"
echo "=========================================="
echo ""

echo "[1/6] 初始化 git 仓库..."
git init

echo "[2/6] 配置提交者信息..."
git config user.name "AI4S Team"
git config user.email "team@ai4s.local"

echo "[3/6] 添加所有文件到暂存区..."
git add .

echo "[4/6] 提交初始版本..."
git commit -m "init: AI4S academic agent full stack v1.0.0" || echo "没有新改动需要提交"

echo "[5/6] 关联远程仓库..."
git remote add origin "$REPO_URL" 2>/dev/null || echo "远程仓库已存在，跳过添加"

echo "[6/6] 推送到 main 分支..."
git branch -M main
if ! git push -u origin main; then
    echo ""
    echo "推送失败，可能是远程仓库已有 README。尝试先拉取再推送..."
    git pull origin main --rebase
    git push -u origin main
fi

echo ""
echo "=========================================="
echo "  完成！代码已推送到：$REPO_URL"
echo "=========================================="
