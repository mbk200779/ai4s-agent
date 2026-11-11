@echo off
setlocal
rem 演示期加固 / 撤销的一键入口。
rem 用法：右键本文件 -> 以管理员身份运行
rem       在后面加 -Revert 可撤销（本文件把参数透传给 ps1）

net session >nul 2>&1
if %errorLevel% neq 0 (
  echo.
  echo   需要管理员权限。
  echo   请右键本文件，选择「以管理员身份运行」。
  echo.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0demo-keepalive.ps1" %*
echo.
pause
