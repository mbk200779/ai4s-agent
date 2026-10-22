@echo off
chcp 65001 >nul
REM 一键把当前代码推送到 GitHub 仓库：https://github.com/mbk200779/ai4s-agent.git
REM 运行前请确保：1. 已安装 Git for Windows；2. 已创建 GitHub 空仓库；3. 知道 GitHub Personal Access Token（PAT）

cd /d "%~dp0"

echo.
echo ==========================================
echo  正在推送代码到 GitHub：mbk200779/ai4s-agent
echo ==========================================
echo.

echo [1/6] 初始化 git 仓库...
git init

echo [2/6] 配置提交者信息...
git config user.name "AI4S Team"
git config user.email "team@ai4s.local"

echo [3/6] 添加所有文件到暂存区...
git add .

echo [4/6] 提交初始版本...
git commit -m "init: AI4S academic agent full stack v1.0.0"

echo [5/6] 关联远程仓库...
git remote add origin https://github.com/mbk200779/ai4s-agent.git 2>nul
if %errorlevel% neq 0 (
    echo 远程仓库已存在，跳过添加
)

echo [6/6] 推送到 main 分支...
git branch -M main
git push -u origin main

if %errorlevel% neq 0 (
    echo.
    echo 推送失败，可能是远程仓库已有 README。尝试先拉取再推送...
    git pull origin main --rebase
    git push -u origin main
)

echo.
echo ==========================================
echo  完成！若上方没有报错，代码已推送成功。
echo  请访问：https://github.com/mbk200779/ai4s-agent
echo ==========================================
pause
