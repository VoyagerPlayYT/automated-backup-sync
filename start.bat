@echo off
chcp 65001 > nul
title Automated Backup and Sync Agent
echo ========================================================
echo   Automated Backup and Sync Agent (SHA-256 Verified)
echo ========================================================
echo [INFO] Запуск центра резервного копирования на http://localhost:8008 ...
start http://localhost:8008
python backup_agent.py
pause
