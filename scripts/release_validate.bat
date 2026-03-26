@echo off
setlocal
cd /d "%~dp0.."
uv run python scripts/release_validate.py %*
exit /b %ERRORLEVEL%
