@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo 처음 실행이라 준비하고 있어요. 1~2분쯤 걸릴 수 있어요...
    py -3 -m venv .venv 2> nul
    if not exist ".venv\Scripts\python.exe" python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
    echo Python을 찾을 수 없어요. https://www.python.org 에서 Python 3.10 이상을 설치한 뒤 다시 실행해 주세요.
    goto fail
)

if not exist ".venv\codevision-ready.txt" (
    echo 필요한 프로그램을 설치하고 있어요...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 (
        echo 설치하지 못했어요. 인터넷 연결을 확인하고 다시 실행해 주세요.
        goto fail
    )
    echo ok> ".venv\codevision-ready.txt"
)

set "PYTHONPATH=%~dp0src"
".venv\Scripts\python.exe" -m codevision %*
if errorlevel 1 goto fail
exit /b 0

:fail
echo.
echo 문제가 생겨서 멈췄어요. 위의 안내를 확인해 주세요.
pause
exit /b 1
