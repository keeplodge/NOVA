@echo off
REM NOVA Algo daily EOD recap — auto-fires at 11:05 ET weekdays via Task Scheduler.
REM Logs each run to logs/eod_recap_YYYY-MM-DD.log so we have an audit trail.

setlocal
cd /d "C:\Users\User\nova\nova-algo-discord"

REM Make sure logs dir exists
if not exist logs mkdir logs

REM Build a date-stamped log filename (YYYY-MM-DD)
for /f "tokens=2 delims==" %%i in ('wmic os get localdatetime /value') do set DT=%%i
set TODAY=%DT:~0,4%-%DT:~4,2%-%DT:~6,2%

set PYTHONIOENCODING=utf-8
python -u post_eod_recap.py >> "logs\eod_recap_%TODAY%.log" 2>&1

endlocal
