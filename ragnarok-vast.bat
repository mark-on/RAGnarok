@echo off
setlocal EnableExtensions DisableDelayedExpansion
title RAGnarok Vast.ai Launcher

set "RAGNAROK_PROJECT=C:\Users\labro\Desktop\Code\Projects\RAGnarok"

echo.
echo RAGnarok Vast.ai Launcher
echo Paste the SSH command shown by Vast.ai, then press Enter.
echo Example: ssh -p 30402 root@82.221.101.16 -L 8080:localhost:8080
echo.

if not defined VAST_SSH_COMMAND set /p "VAST_SSH_COMMAND=Vast.ai SSH command: "
if not defined VAST_SSH_COMMAND goto :invalid_command

set "SSH_PORT="
set "SSH_TARGET="
for /f "tokens=1,2 delims=," %%A in ('powershell -NoProfile -Command "$s=$env:VAST_SSH_COMMAND -replace '\\@','@'; $p=[regex]::Match($s,'(?i)(?:^|\s)-p\s+(\d+)').Groups[1].Value; $t=[regex]::Match($s,'(?i)(?:^|\s)([a-z0-9._-]+@[a-z0-9.-]+)(?=\s|$)').Groups[1].Value; if($p -and $t){Write-Output ($p+','+$t)}"') do (
    set "SSH_PORT=%%A"
    set "SSH_TARGET=%%B"
)

if not defined SSH_PORT goto :invalid_command
if not defined SSH_TARGET goto :invalid_command

echo.
echo SSH target: %SSH_TARGET%
echo SSH port:   %SSH_PORT%

if /i "%RAGNAROK_VAST_DRY_RUN%"=="1" exit /b 0

where ssh.exe >nul 2>&1
if errorlevel 1 (
    echo.
    echo ERROR: Windows OpenSSH was not found.
    echo Install "OpenSSH Client" from Windows Optional Features and retry.
    goto :failed
)

echo.
echo [1/4] Stopping local Ollama and preparing port 11434...
powershell -NoProfile -Command "$ollama=Get-Process -Name 'ollama*' -ErrorAction SilentlyContinue; if($ollama){Write-Host ('Stopping local Ollama process(es): '+(($ollama.Id | Sort-Object -Unique) -join ', ')); $ollama | Stop-Process -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 1}; $listeners=Get-NetTCPConnection -LocalPort 11434 -State Listen -ErrorAction SilentlyContinue; if(-not $listeners){Write-Host 'Local port 11434 is ready.'; exit 0}; foreach($listener in $listeners){$owner=Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue; $name=if($owner){$owner.ProcessName}else{'unknown'}; Write-Host ('Port 11434 is still used by PID '+$listener.OwningProcess+' ('+$name+').')}; exit 1"
if errorlevel 1 (
    echo.
    echo ERROR: local port 11434 is already in use.
    echo Close the process shown above or any previous SSH tunnel, then retry.
    goto :failed
)
if /i "%RAGNAROK_VAST_PORT_CHECK_ONLY%"=="1" exit /b 0

echo [2/4] Connecting to Vast.ai and preparing Ollama...
echo The first connection may ask you to confirm the server fingerprint.
ssh -o ServerAliveInterval=30 -p %SSH_PORT% %SSH_TARGET% "set -e; if ! command -v ollama >/dev/null 2>&1; then echo 'Installing Ollama...'; curl -fsSL https://ollama.com/install.sh | sh; else echo 'Ollama is already installed.'; fi; if ! curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then echo 'Starting Ollama API...'; nohup ollama serve >$HOME/ollama.log 2>&1 </dev/null & fi; i=0; while [ $i -lt 45 ]; do if curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then echo 'Ollama API is ready.'; exit 0; fi; i=$((i+1)); sleep 1; done; echo 'Ollama did not become ready. Remote log:'; if [ -f $HOME/ollama.log ]; then tail -n 40 $HOME/ollama.log; else echo 'No Ollama log was created.'; fi; exit 1"
if errorlevel 1 (
    echo.
    echo ERROR: remote Ollama setup failed. Review the messages above.
    goto :failed
)

echo [3/4] Opening the remote shell with the persistent Ollama tunnel...
start "RAGnarok - KEEP OPEN - Vast SSH + Ollama Tunnel" cmd.exe /d /k "title RAGnarok - KEEP OPEN - Vast SSH + Ollama Tunnel & color 1F & echo REMOTE VAST.AI SHELL AND OLLAMA TUNNEL. & echo KEEP THIS WINDOW OPEN DURING INFERENCE. & echo Use ollama pull and ollama list here when needed. & echo. & ssh.exe -tt -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 127.0.0.1:11434:127.0.0.1:11434 -p %SSH_PORT% %SSH_TARGET%"
powershell -NoProfile -Command "$ready=$false; foreach($attempt in 1..15){try{$null=Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2; $ready=$true; break}catch{Start-Sleep -Seconds 1}}; if($ready){Write-Host 'Local tunnel verified.'; exit 0}; Write-Host 'The local tunnel did not become ready.'; exit 1"
if errorlevel 1 (
    echo.
    echo ERROR: the Ollama tunnel failed to start.
    echo Review the blue Vast SSH window for the error.
    goto :failed
)

echo [4/4] Opening the local RAGnarok terminal...
start "RAGnarok - Local Framework" cmd.exe /d /k "title RAGnarok - Local Framework & color 0A & cd /d %RAGNAROK_PROJECT% & call .venv\Scripts\activate.bat & echo. & echo LOCAL RAGNAROK TERMINAL & echo Remote Ollama is available at http://127.0.0.1:11434 & echo Start with: ragnarok run"

echo.
echo Ready. Keep the blue "RAGnarok - KEEP OPEN - Vast SSH + Ollama Tunnel" window open.
echo Closing it also closes the Ollama tunnel.
timeout /t 2 /nobreak >nul
exit /b 0

:invalid_command
echo.
echo ERROR: the command must contain both "-p PORT" and "user@host".
echo Example: ssh -p 30402 root@82.221.101.16
goto :failed

:failed
echo.
pause
exit /b 1
