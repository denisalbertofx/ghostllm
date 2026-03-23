@echo off
set PYTHONPATH=%~dp0;%~dp0packages\py-core
python "%~dp0apps\cli\main.py" %*
