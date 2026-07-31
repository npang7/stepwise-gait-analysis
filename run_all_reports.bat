@echo off
setlocal

REM StepWise batch report runner.
REM Put this .bat file in the same folder as:
REM   stepwise_reference_pipeline.py
REM   stepwise_gait_analysis.py
REM   standing.txt
REM   forefoot.txt / rearfoot.txt / inversion.txt / eversion.txt / normal.txt

cd /d "%~dp0"

set "PYTHON_EXE=python"
set "SCRIPT=stepwise_reference_pipeline.py"
set "STANDING=standing.txt"
if defined STEPWISE_OUTPUT_DIR (
  set "OUT_ROOT=%STEPWISE_OUTPUT_DIR%"
) else (
  set "OUT_ROOT=%CD%\stepwise_cli_reports"
)

REM Current sensor mapping:
REM   P1 = lateral forefoot / little-toe root
REM   P2 = heel
REM   P3 = arch
REM   P4 = medial forefoot / big-toe root
set "MAPPING=--heel P2 --arch P3 --medial-forefoot P4 --lateral-forefoot P1 --pitch-eversion-sign positive"

if not exist "%SCRIPT%" (
  echo ERROR: Cannot find %SCRIPT% in this folder.
  echo Current folder: %CD%
  pause
  exit /b 1
)

if not exist "%STANDING%" (
  echo ERROR: Cannot find standing calibration file: %STANDING%
  echo Put standing.txt in this folder, or edit STANDING in this bat file.
  pause
  exit /b 1
)

if not exist "%OUT_ROOT%" mkdir "%OUT_ROOT%"

call :run_one "normal" "normal.txt"
call :run_one "forefoot" "forefoot.txt"
call :run_one "rearfoot" "rearfoot.txt"
call :run_one "inversion" "inversion.txt"
call :run_one "eversion" "eversion.txt"

echo.
echo All available StepWise reports finished.
echo Output root:
echo %OUT_ROOT%
pause
exit /b 0

:run_one
set "NAME=%~1"
set "INPUT=%~2"

if not exist "%INPUT%" (
  echo.
  echo SKIP: %INPUT% not found.
  exit /b 0
)

echo.
echo ===== Running %NAME%: %INPUT% =====
"%PYTHON_EXE%" "%SCRIPT%" "%INPUT%" --output-dir "%OUT_ROOT%\%NAME%" --standing-file "%STANDING%" %MAPPING%

if errorlevel 1 (
  echo ERROR: Failed while processing %INPUT%.
  pause
  exit /b 1
)

echo DONE: %OUT_ROOT%\%NAME%\user_report.pdf
exit /b 0
