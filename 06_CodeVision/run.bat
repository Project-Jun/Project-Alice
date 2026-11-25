@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0"

set "CV_FIRST="
if not exist ".venv\Scripts\python.exe" (
    set "CV_FIRST=1"
    rem A marker left from an earlier venv must not skip the install into this new one.
    del /q ".venv\codevision-ready.txt" > nul 2> nul
    echo 처음 실행이라 준비하고 있어요. 1~2분쯤 걸릴 수 있어요...
    py -3 -m venv .venv 2> nul
    if not exist ".venv\Scripts\python.exe" python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
    echo Python을 찾을 수 없어요. https://www.python.org 에서 Python 3.10 이상을 설치한 뒤 다시 실행해 주세요.
    goto fail
)

rem The marker is a copy of the requirements.txt that was installed, so a changed requirements.txt installs again.
rem Only a first install must succeed. Later, a failed install warns, keeps the old marker (the next start tries
rem again) and starts CodeVision anyway.
if not exist ".venv\codevision-ready.txt" set "CV_FIRST=1"
"%SystemRoot%\System32\fc.exe" /b requirements.txt ".venv\codevision-ready.txt" > nul 2> nul
if errorlevel 1 (
    echo 필요한 프로그램을 설치하고 있어요...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 (
        if defined CV_FIRST (
            echo 설치하지 못했어요. 인터넷 연결을 확인하고 다시 실행해 주세요.
            goto fail
        )
        echo 새 기능에 필요한 프로그램을 설치하지 못했어요. 지금은 그대로 켜요.
    ) else (
        copy /y requirements.txt ".venv\codevision-ready.txt" > nul
    )
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
