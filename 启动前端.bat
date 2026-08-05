@echo off
chcp 65001 > nul
REM A-Share Trend Compass 一键启动静态服务器
cd /d "%~dp0\web"
echo ============================================
echo   A 股趋势罗盘 - 本地预览
echo ============================================
echo.
echo 访问地址: http://127.0.0.1:8765/index.html
echo.
echo 按 Ctrl+C 停止服务器
echo ============================================
echo.
start "" "http://127.0.0.1:8765/index.html"
python -m http.server 8765
pause
