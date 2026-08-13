@echo off
cd /d "%~dp0"
title 변환대

where python >nul 2>&1
if errorlevel 1 (
  echo Python이 설치되어 있지 않습니다.
  echo python.org 에서 설치할 때 Add Python to PATH 를 반드시 체크하세요.
  pause
  exit /b
)

python -c "import flask, pypdf" >nul 2>&1
if errorlevel 1 (
  echo 필요한 패키지를 설치합니다. 잠시만 기다려주세요.
  python -m pip install -r requirements.txt
)

echo.
echo 브라우저가 열립니다. 이 창을 닫으면 프로그램도 종료됩니다.
echo.
python app.py
pause
