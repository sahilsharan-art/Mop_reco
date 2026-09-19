@echo off
REM ---------------------------------------------------------------------------
REM  Citykart MOP Reconciliation - start the dashboard
REM  Double-click this file. It starts the app and opens it in your browser.
REM  Close this black window to stop the dashboard.
REM ---------------------------------------------------------------------------

title MOP Reconciliation - dashboard
cd /d "%~dp0"

echo.
echo  ==========================================================
echo   Citykart MOP Reconciliation
echo  ==========================================================
echo   Cash reconciliation to follow.
echo.
echo   Starting the dashboard...
echo   Keep this window open while you use it.
echo   Close this window to stop the dashboard.
echo.

REM Find a working Python. "py" is the Windows launcher; fall back to python.
set "PYTHON_CMD="
where py >nul 2>&1 && set "PYTHON_CMD=py"
if not defined PYTHON_CMD (
    where python >nul 2>&1 && set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
    echo   [ERROR] Python was not found on this machine.
    echo.
    echo   What to do: install Python 3.14, then run this file again.
    echo.
    pause
    exit /b 1
)

REM Check Streamlit is installed before trying to launch it.
%PYTHON_CMD% -c "import streamlit" >nul 2>&1
if errorlevel 1 (
    echo   [ERROR] The required Python packages are not installed.
    echo.
    echo   What to do: open a command prompt in this folder and run:
    echo       %PYTHON_CMD% -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

REM ---------------------------------------------------------------------------
REM  Stop any dashboard that is ALREADY running before starting a new one.
REM
REM  This is not tidiness. Streamlit re-runs the page scripts on every
REM  interaction but keeps imported modules in memory, so a process started
REM  before a code change goes on serving the OLD module with the NEW page
REM  script. That combination raises AttributeError in the middle of a page
REM  and makes a change look like it was never made. Leaving an old process
REM  bound to 8501 is therefore a silent-wrong-answer bug, not clutter.
REM ---------------------------------------------------------------------------
echo   Checking for a dashboard that is already running...
REM  Delegated to Python: the inline PowerShell that used to live here had
REM  nested-quoting problems, ran silently, and killed nothing — which is how a
REM  two-day-old process went on serving port 8501 with stale modules.
%PYTHON_CMD% "scripts\stop_dashboards.py"
echo.

echo   Opening http://localhost:8501 in your browser...
echo.

REM Give the server a moment to bind the port, then open the browser.
start "" cmd /c "timeout /t 5 >nul & start http://localhost:8501"

%PYTHON_CMD% -m streamlit run "app\Summary.py" --server.port 8501 --server.headless true

REM If Streamlit exits with an error, keep the window open so the reason is readable.
if errorlevel 1 (
    echo.
    echo   [ERROR] The dashboard stopped unexpectedly. The reason is above.
    echo.
    pause
)
