@echo off
rem Starts the ActiveSG gym bot in a minimized window. Close that window to stop it.
rem If the bot exits with an error, the window stays open so you can read why.
cd /d "%~dp0"
start "ActiveSG gym bot" /min cmd /c "py gymbot.py || pause"
