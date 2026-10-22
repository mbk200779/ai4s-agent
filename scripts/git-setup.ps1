# 一键初始化 git 仓库并推送到远程（GitHub / Gitee）
# 用法：.\scripts\git-setup.ps1 -RepoUrl "https://github.com/用户名/仓库名.git"

param(
    [Parameter(Mandatory=$true)]
    [string]$RepoUrl
)

Write-Host "==> 初始化 git 仓库..."
git init

Write-Host "==> 配置 git 用户信息..."
git config user.name "AI4S Team"
git config user.email "team@ai4s.local"

Write-Host "==> 添加所有文件..."
git add .

Write-Host "==> 提交初始版本..."
git commit -m "init: AI4S academic agent full stack v1.0.0"

Write-Host "==> 添加远程仓库..."
git remote add origin $RepoUrl 2>$null
if (-not $?) {
    Write-Host "远程仓库已存在，跳过添加"
}

Write-Host "==> 推送到 main 分支..."
git branch -M main
git push -u origin main

Write-Host "==> 完成！仓库地址：$RepoUrl"
Write-Host "提示：如需体现真实开发过程，可后续用 git rebase -i 或分模块补提交。"
