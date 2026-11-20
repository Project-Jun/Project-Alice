@echo off
chcp 65001 > nul
echo ====================================
echo  AI 개인 비서 - 설치 및 실행 스크립트
echo ====================================
echo.

REM Python 확인
python --version 2>NUL
if errorlevel 1 (
    echo [오류] Python이 설치되어 있지 않습니다.
    echo Python 3.10 이상을 설치해주세요: https://www.python.org
    pause
    exit /b 1
)

REM 가상환경 생성
if not exist "venv" (
    echo [1/3] 가상환경 생성 중...
    python -m venv venv
)

REM 가상환경 활성화
call venv\Scripts\activate.bat

REM 패키지 설치
echo [2/3] 패키지 설치 중 (처음 실행 시 시간이 걸립니다)...
pip install -r requirements.txt -q

REM data 폴더 생성
if not exist "data" mkdir data

echo [3/3] AI 비서 시작!
echo.
echo ⚠️  처음 실행 전에 config.yaml에서 API 키를 설정하세요.
echo.
python main.py

pause
