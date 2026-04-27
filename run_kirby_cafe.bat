@echo off
cd /d "%~dp0"
"C:\Users\mitsu\AppData\Local\Programs\Python\Python314\python.exe" check_kirby_cafe.py >> "%~dp0run_log.txt" 2>&1
