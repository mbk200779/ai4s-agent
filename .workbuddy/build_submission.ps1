$src = "D:\workbuddy\2026-10-21-16-54-36"
$dst = "C:\Users\36574\Desktop\传智杯"

# 清理旧目录
if (Test-Path $dst) { Remove-Item $dst -Recurse -Force -ErrorAction SilentlyContinue }

$dirs = @(
    "作品文档", "作品文档\assets",
    "作品源码", "作品源码\backend", "作品源码\frontend",
    "作品源码\docs", "作品源码\scripts",
    "演示与评测", "演示与评测\评测报告"
)
foreach ($d in $dirs) { New-Item -ItemType Directory -Force -Path (Join-Path $dst $d) | Out-Null }

# 1. 作品文档（用户改过的优先）
Copy-Item "C:\Users\36574\Desktop\技术文档.md" (Join-Path $dst "作品文档\技术文档.md") -Force
Copy-Item "C:\Users\36574\Desktop\README.md" (Join-Path $dst "作品文档\README.md") -Force
Copy-Item "$src\docs\ARCHITECTURE.md" (Join-Path $dst "作品文档\ARCHITECTURE.md") -Force
Copy-Item "$src\docs\TECH_DOC.md" (Join-Path $dst "作品文档\TECH_DOC.md") -Force
Copy-Item "$src\docs\DEMO_SCRIPT.md" (Join-Path $dst "作品文档\DEMO_SCRIPT.md") -Force
Copy-Item "$src\docs\SUBMISSION_CHECKLIST.md" (Join-Path $dst "作品文档\SUBMISSION_CHECKLIST.md") -Force
Copy-Item "$src\docs\assets\generate_defense_image.png" (Join-Path $dst "作品文档\assets\generate_defense_image.png") -Force

# 2. 作品源码 - 根文件
Copy-Item "$src\docker-compose.yml" (Join-Path $dst "作品源码\docker-compose.yml") -Force
Copy-Item "$src\Makefile" (Join-Path $dst "作品源码\Makefile") -Force
Copy-Item "$src\.env.example" (Join-Path $dst "作品源码\.env.example") -Force
Copy-Item "$src\.gitignore" (Join-Path $dst "作品源码\.gitignore") -Force
Copy-Item "$src\README.md" (Join-Path $dst "作品源码\README.md") -Force

# backend（排除 __pycache__、*.pyc、*.log）
robocopy "$src\backend" (Join-Path $dst "作品源码\backend") /E /XD __pycache__ /XF *.pyc *.log /NFL /NDL /NJH /NJS | Out-Null

# frontend（排除 node_modules、.next、out）
robocopy "$src\frontend" (Join-Path $dst "作品源码\frontend") /E /XD node_modules .next out /NFL /NDL /NJH /NJS | Out-Null

# docs
robocopy "$src\docs" (Join-Path $dst "作品源码\docs") /E /NFL /NDL /NJH /NJS | Out-Null

# scripts
robocopy "$src\scripts" (Join-Path $dst "作品源码\scripts") /E /NFL /NDL /NJH /NJS | Out-Null

# 3. 演示与评测
Copy-Item "$src\backend\eval\golden_qa.json" (Join-Path $dst "演示与评测\golden_qa.json") -Force
if (Test-Path "$src\backend\eval\run1.md") { Copy-Item "$src\backend\eval\run1.md" (Join-Path $dst "演示与评测\评测报告\run1.md") -Force }
if (Test-Path "$src\backend\eval\run2.md") { Copy-Item "$src\backend\eval\run2.md" (Join-Path $dst "演示与评测\评测报告\run2.md") -Force }

# 结果统计
$count = (Get-ChildItem $dst -Recurse -File | Measure-Object).Count
"DONE count=$count" | Out-File "D:\workbuddy\2026-10-21-16-54-36\.workbuddy\build_result.txt" -Encoding utf8
