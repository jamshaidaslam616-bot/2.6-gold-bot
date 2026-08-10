# Supervisor for the 2.6 gold bot.
#
# Restarts the daemon if it exits for any reason, with a backoff so a
# permanently broken install does not spin the CPU. It deliberately does NOT
# restart after a clean exit (code 0) — that is the kill switch or a Ctrl+C,
# and both mean a human wanted it stopped.
#
# Usage:   powershell -ExecutionPolicy Bypass -File C:\gold-2.6-bot\run.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
$bot = Join-Path $root "main.py"
$log = Join-Path $root "logs\supervisor.log"

New-Item -ItemType Directory -Force -Path (Join-Path $root "logs") | Out-Null

function Write-Log($message) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $message
    Write-Output $line
    Add-Content -Path $log -Value $line -Encoding utf8
}

if (-not (Test-Path $python)) {
    Write-Log "FATAL: no venv at $python. Run: python -m venv .venv; .venv\Scripts\python.exe -m pip install -r requirements.txt"
    exit 1
}
if (-not (Test-Path (Join-Path $root ".env"))) {
    Write-Log "FATAL: no .env. Copy .env.example to .env and fill it in."
    exit 1
}

$attempt = 0
while ($true) {
    Write-Log "starting main.py (attempt $($attempt + 1))"
    & $python $bot
    $code = $LASTEXITCODE

    if ($code -eq 0) {
        Write-Log "clean exit (0) - a human stopped it, or the kill switch fired. Not restarting."
        break
    }

    $attempt++
    # 15s, 30s, 60s, 120s, then hold at 300s. A bridge that keeps failing is a
    # bridge in an unknown state; hammering it does not help.
    $wait = [Math]::Min(15 * [Math]::Pow(2, [Math]::Min($attempt - 1, 4)), 300)
    Write-Log "exited with code $code - restarting in $wait seconds"
    Start-Sleep -Seconds $wait
}
