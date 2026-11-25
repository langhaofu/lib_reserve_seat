@echo off
chcp 65001 >nul
echo.
echo ╔═══════════════════════════════════════════╗
echo ║   成都信息工程大学图书馆 自动抢座脚本    ║
echo ╚═══════════════════════════════════════════╝
echo.
echo 【使用方法】
echo 1. 先关闭所有 Chrome 浏览器窗口
echo 2. 点击确定/按回车，脚本将自动启动 Chrome
echo 3. 在 Chrome 中登录图书馆系统
echo 4. 脚本会在凌晨自动帮你抢座
echo.
pause
python reserve.py
echo.
pause
