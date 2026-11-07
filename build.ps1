# Build PDF2Audiobook (folder distribution + zip) and smoke-test the exe.
# Usage:  powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Name = "PDF2Audiobook"
$Exe = Join-Path $Root "dist\$Name\$Name.exe"

Write-Host "==> Running unit tests..."
python -m pytest -q (Join-Path $Root "tests")
if ($LASTEXITCODE -ne 0) { throw "Tests failed" }

Write-Host "==> Building $Name with PyInstaller..."
python (Join-Path $Root "scripts\build_exe.py")
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $Exe)) {
    throw "Build failed: see build\build_log.txt"
}

# Smoke test: the GUI exe must stay alive (no instant crash on startup).
Write-Host "==> Smoke test: launching $Name for 10 seconds..."
$proc = Start-Process -FilePath $Exe -PassThru
Start-Sleep -Seconds 10
if ($proc.HasExited) {
    throw "Smoke test FAILED: exe exited early with code $($proc.ExitCode) (see %LOCALAPPDATA%\PDF2Audiobook\logs\app.log)"
}
Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
Write-Host "==> Smoke test passed (exe stayed alive)."
Write-Host ""
Write-Host "Recommended manual check before a release: convert a real PDF, play it,"
Write-Host "change speed (Ctrl+Up/Down), open 'Read PDF', export an M4B and restart the app."
Write-Host ""
Write-Host "Build complete: $Exe"
