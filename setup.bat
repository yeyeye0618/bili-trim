@echo off
chcp 65001 >nul
rem 給「不想開終端機、想點兩下」的人用的。做的事只有一件：
rem 找到一個能用的 Python，然後把 setup.py 交給它。
rem
rem 為什麼不直接叫使用者打 python setup.py：在 Windows 上
rem   - 有人只有 py（python.org 的啟動器），打 python 會跳到市集
rem   - 有人裝了 Microsoft Store 版，python 指向一個空殼
rem   - 有人兩個都有，而套件裝在另一個上面
rem 下面的順序就是「最可能是正常安裝的那一個」排前面。
rem
rem 這個檔案一定要存成 UTF-8 + CRLF。換行用 LF 的話 cmd.exe 的 goto
rem 會接不到標籤，整個檔案會以很難看懂的方式壞掉（實測過）。

cd /d "%~dp0"

where py >nul 2>&1
if %errorlevel%==0 goto :usepy

where python >nul 2>&1
if %errorlevel%==0 goto :usepython

echo.
echo   這台電腦還沒有 Python。裝好之後再點一次這個檔案：
echo.
echo       winget install -e --id Python.Python.3.12
echo.
echo   或到 https://www.python.org/downloads/ 下載，
echo   安裝畫面最下面記得勾「Add python.exe to PATH」。
echo.
pause
exit /b 1

:usepy
py -3 setup.py %*
goto :end

:usepython
python setup.py %*
goto :end

:end
echo.
pause
