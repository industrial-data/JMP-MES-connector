@echo off
setlocal

set server_DA_address="servername"

rem Remember start time. Note that we don't look at the date, so this
rem calculation won't work right if the program run spans local midnight.
set t0=%time: =0%

rem do something here.... but probably with more care about quoting.
rem specifically, odd things will happen if any arguments contain 
rem percent signs or carets and there may be no way to prevent it.
%*

powershell -ExecutionPolicy Bypass -File .\OLEDB_extract.ps1 %server_DA_address% "SELECT format(date('*'), 'yyyy-MM-dd HH:mm:ss') as Current_Server_Time;"

rem Capture the end time before doing anything else
set t=%time: =0%

rem make t0 into a scaler in 100ths of a second, being careful not 
rem to let SET/A misinterpret 08 and 09 as octal
set /a h=1%t0:~0,2%-100
set /a m=1%t0:~3,2%-100
set /a s=1%t0:~6,2%-100
set /a c=1%t0:~9,2%-100
set /a starttime = %h% * 360000 + %m% * 6000 + 100 * %s% + %c%

rem make t into a scaler in 100ths of a second
set /a h=1%t:~0,2%-100
set /a m=1%t:~3,2%-100
set /a s=1%t:~6,2%-100
set /a c=1%t:~9,2%-100
set /a endtime = %h% * 360000 + %m% * 6000 + 100 * %s% + %c%

rem runtime in 100ths is now just end - start
set /a runtime = %endtime% - %starttime%
set runtime = %s%.%c%

echo Started at %t0%
echo Ended at %t%
echo -------
echo %server_DA_address%
echo PI Server connection took %runtime%0 [ms]