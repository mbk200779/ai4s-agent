<#
.SYNOPSIS
    演示期临时加固：让这台笔记本在答辩那几天不自己睡着、不自己重启。

.DESCRIPTION
    解决的是「服务本来好好的，机器自己出状况」这一类问题：
      1. Windows 更新在半夜装完自动重启 —— 第二天来了网页打不开
      2. 合盖 / 空闲进入睡眠 —— 网络断开，隧道掉线
      3. 网卡省电关闭 —— 长时间无流量后唤醒失败
      4. 唤醒定时器 —— 半夜被计划任务唤醒又睡回去，隧道重连不稳

    只做这几件事，**不动** Windows Update 服务本身（停用它会被系统自动恢复，
    而且会让机器长期不打补丁）。到期自动恢复，或者用 -Revert 立刻撤销。

.PARAMETER Days
    暂停更新的天数，默认 7。演示窗口有多长就设多长。

.PARAMETER Revert
    撤销本脚本做过的全部改动，恢复到系统默认。

.PARAMETER InstallBootTask
    额外注册一个「登录后 2 分钟自检」的计划任务，容器没起来就自动拉起。

.EXAMPLE
    演示前一周执行（在管理员 PowerShell 里）：
        .\deploy\demo-keepalive.ps1 -Days 7

.EXAMPLE
    演示结束后恢复：
        .\deploy\demo-keepalive.ps1 -Revert
#>
[CmdletBinding()]
param(
    [int]$Days = 7,
    [switch]$Revert,
    [switch]$InstallBootTask
)

$ErrorActionPreference = 'Stop'
$script:done = @()
$script:skip = @()

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Good($msg) { Write-Host "  [OK]   $msg" -ForegroundColor Green; $script:done += $msg }
function Warn2($msg) { Write-Host "  [SKIP] $msg" -ForegroundColor Yellow; $script:skip += $msg }

# ---------- 0. 权限 ----------
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host ""
    Write-Host " 需要管理员权限。" -ForegroundColor Red
    Write-Host " 做法：开始菜单搜 PowerShell -> 右键「以管理员身份运行」-> 再执行本脚本。" -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

$UX = 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings'

# ---------- 1. 暂停 Windows 更新（防半夜自动重启） ----------
Step "Windows 更新暂停"

if ($Revert) {
    if (Test-Path $UX) {
        foreach ($n in 'PauseUpdatesStartTime','PauseUpdatesExpiryTime',
                       'PauseFeatureUpdatesStartTime','PauseFeatureUpdatesEndTime',
                       'PauseQualityUpdatesStartTime','PauseQualityUpdatesEndTime') {
            Remove-ItemProperty -Path $UX -Name $n -ErrorAction SilentlyContinue
        }
    }
    # 让 Windows 重新拉一次策略
    Good "已清除暂停标记，更新恢复正常"
}
else {
    $now = (Get-Date).ToUniversalTime()
    $end = $now.AddDays($Days)
    # 六个值要一起写才生效：只写 ExpiryTime，系统会认为「没有开始时间」而忽略。
    $set = @{
        PauseUpdatesStartTime        = $now.ToString('yyyy-MM-ddTHH:mm:ssZ')
        PauseUpdatesExpiryTime       = $end.ToString('yyyy-MM-ddTHH:mm:ssZ')
        PauseFeatureUpdatesStartTime = $now.ToString('yyyy-MM-ddTHH:mm:ssZ')
        PauseFeatureUpdatesEndTime   = $end.ToString('yyyy-MM-ddTHH:mm:ssZ')
        PauseQualityUpdatesStartTime = $now.ToString('yyyy-MM-ddTHH:mm:ssZ')
        PauseQualityUpdatesEndTime   = $end.ToString('yyyy-MM-ddTHH:mm:ssZ')
    }
    if (-not (Test-Path $UX)) { New-Item -Path $UX -Force | Out-Null }
    foreach ($k in $set.Keys) {
        New-ItemProperty -Path $UX -Name $k -Value $set[$k] -PropertyType String -Force | Out-Null
    }
    Good ("更新已暂停到 {0}（{1} 天）" -f $end.ToLocalTime().ToString('yyyy-MM-dd HH:mm'), $Days)
}

# ---------- 2. 唤醒定时器 ----------
Step "唤醒定时器"

try {
    if ($Revert) {
        powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1 | Out-Null
        Good "唤醒定时器恢复为「启用」"
    }
    else {
        powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 0 | Out-Null
        Good "唤醒定时器已禁用（避免半夜被更新任务唤醒后隧道重连不稳）"
    }
    powercfg /setactive SCHEME_CURRENT | Out-Null
}
catch { Warn2 "唤醒定时器设置失败：$($_.Exception.Message)" }

# ---------- 3. 网卡省电 ----------
Step "网卡省电"

$nics = Get-NetAdapter -Physical -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq 'Up' }
if (-not $nics) { $nics = Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq 'Up' } }
if ($nics) {
    foreach ($n in $nics) {
        try {
            if ($Revert) { Enable-NetAdapterPowerManagement -Name $n.Name -ErrorAction Stop }
            else         { Disable-NetAdapterPowerManagement -Name $n.Name -ErrorAction Stop }
            Good ("{0}（{1}）{2}省电关闭" -f $n.Name, $n.InterfaceDescription,
                  $(if ($Revert) { '恢复' } else { '已' }))
        }
        catch { Warn2 "$($n.Name) 电源管理不可调（驱动不支持，不影响演示）" }
    }
}
else { Warn2 "没有找到已连接的网卡" }

# ---------- 4. 睡眠与合盖 ----------
Step "睡眠与合盖"

if ($Revert) {
    powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 1800 | Out-Null
    powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP HIBERNATEIDLE 7200 | Out-Null
    powercfg /setactive SCHEME_CURRENT | Out-Null
    Good "空闲睡眠恢复为 30 分钟 / 2 小时"
}
else {
    powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 0 | Out-Null
    powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP HIBERNATEIDLE 0 | Out-Null
    powercfg /setactive SCHEME_CURRENT | Out-Null
    Good "插电时永不睡眠 / 永不休眠"

    # 合盖动作：Modern Standby(S0) 机型在部分固件上不暴露 LIDACTION，
    # 设不进去就如实说，让用户手动点一次，而不是假装成功。
    $lidOut = (powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0 2>&1) -join ' '
    powercfg /setactive SCHEME_CURRENT 2>&1 | Out-Null
    $lidNow = (powercfg /query SCHEME_CURRENT SUB_BUTTONS LIDACTION 2>&1) -join ' '
    if ($lidNow -match '0x00000000') {
        Good "合盖不进入睡眠"
    }
    else {
        Warn2 "本机固件未开放「合盖动作」设置，需手动改：控制面板 -> 电源选项 -> 选择关闭盖子的功能 -> 插电时「不采取任何操作」"
    }
}

# ---------- 5. 可选：登录后自动拉起容器 ----------
if ($InstallBootTask) {
    Step "开机兜底任务"
    $taskName = 'AI4S-DemoKeepAlive'
    $root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
    $check = Join-Path $root 'deploy\demo-check.sh'

    # 找 bash：优先 Git for Windows，Docker Desktop 自带的不带完整 coreutils
    $bash = @(
        'C:\Program Files\Git\bin\bash.exe',
        'C:\Program Files (x86)\Git\bin\bash.exe'
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1

    if (-not $bash) {
        Warn2 "没找到 Git Bash，跳过兜底任务（手动跑：bash deploy\demo-check.sh）"
    }
    elseif (-not (Test-Path $check)) {
        Warn2 "没找到 deploy\demo-check.sh，跳过"
    }
    else {
        $act = New-ScheduledTaskAction -Execute $bash `
               -Argument "-lc 'cd `"$($root -replace '\\','/')`" && bash deploy/start-all.sh > /tmp/ai4s-boot.log 2>&1'"
        $trg = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $trg.Delay = 'PT2M'
        $prn = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
        $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
               -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
        Register-ScheduledTask -TaskName $taskName -Action $act -Trigger $trg `
            -Principal $prn -Settings $set -Force -Description '登录后 2 分钟把 AI4S 容器拉起来' | Out-Null
        Good "已注册计划任务 $taskName（登录后 2 分钟执行，日志 /tmp/ai4s-boot.log）"
    }
}

# ---------- 结论 ----------
Write-Host ""
Write-Host ("=" * 56)
if ($Revert) { Write-Host " 已撤销演示期加固，系统恢复默认" }
else         { Write-Host " 演示期加固完成" }
Write-Host ("=" * 56)
if ($script:done.Count) { Write-Host " 生效项："; $script:done | ForEach-Object { Write-Host "   - $_" } }
if ($script:skip.Count) { Write-Host " 跳过项（需手动处理）："; $script:skip | ForEach-Object { Write-Host "   - $_" } }
Write-Host ""
Write-Host " 演示前自检：bash deploy/demo-check.sh" -ForegroundColor Cyan
Write-Host " 演示后还原：.\deploy\demo-keepalive.ps1 -Revert" -ForegroundColor Cyan
Write-Host ""
