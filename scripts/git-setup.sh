#!/bin/bash
# 一键初始化 git 仓库并推送到远程（GitHub / Gitee）
# 用法：bash scripts/git-setup.sh https://github.com/用户名/仓库名.git

set -e

REPO_URL="${1:-}"

if [ -z "$REPO_URL" ]; then
    echo "用法：bash scripts/git-setup.sh <仓库地址>"
    echo "示例：bash scripts/git-setup.sh https://github.com/my-team/ai4s-agent.git"
    exit 1
fi

echo "==> 初始化 git 仓库..."
git init

echo "==> 配置 git 用户信息..."
git config user.name "AI4S Team"
git config user.email "team@ai4s.local"

echo "==> 添加所有文件..."
git add .

echo "==> 提交初始版本..."
git commit -m "init: AI4S academic agent full stack v1.0.0"

echo "==> 添加远程仓库..."
git remote add origin "$REPO_URL" || echo "远程仓库已存在，跳过添加"

echo "==> 推送到 main 分支..."
git branch -M main
git push -u origin main

echo "==> 完成！仓库地址：$REPO_URL"
echo "提示：如需体现真实开发过程，可后续用 'git rebase -i' 或分模块补提交。"
