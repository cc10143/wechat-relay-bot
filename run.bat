@echo off
chcp 65001 >nul
echo ========================================
echo    微信自动接龙机器人 - 启动中...
echo ========================================
echo.
"D:\KaiFa\Python\python.exe" "%~dp0wechat_relay_bot.py"
pause
