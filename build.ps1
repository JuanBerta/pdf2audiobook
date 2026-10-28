# Build PDF2Audiobook into a standalone exe and smoke-test it.
# Usage:  powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Name = "PDF2Audiobook"
$Entry = Join-Path $Root "app\main.py"
$Dist = Join-Path $Root "dist"
$Exe = Join-Path $Dist "$Name.exe"

Write-Host "==> Building $Name with PyInstaller..."
python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name $Name `
    --collect-all pymupdf `
    --collect-all piper `
    --collect-all piper_phonemize `
    --collect-all onnxruntime `
    --collect-all soundfile `
    --hidden-import sounddevice `
    $Entry

if (-not (Test-Path $Exe)) {
    throw "Build failed: $Exe not found"
}
Write-Host "==> Exe produced: $Exe"

# Smoke test 1: the GUI exe must stay alive (no instant crash on startup).
Write-Host "==> Smoke test: launching $Name for 10 seconds..."
$proc = Start-Process -FilePath $Exe -PassThru
Start-Sleep -Seconds 10
if ($proc.HasExited) {
    throw "Smoke test FAILED: exe exited early with code $($proc.ExitCode)"
}
Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
Write-Host "==> Smoke test passed (exe stayed alive)."

# Smoke test 2 (manual, needs a real PDF + voice model):
Write-Host ""
Write-Host "==> Optional functional smoke test (recommended before release):"
Write-Host "    Install the app, add a Piper voice model, then convert a real PDF."
Write-Host "    Verify chapters play, speed buttons (Ctrl+Up/Down) change rate,"
Write-Host "    and the library survives a restart."
Write-Host ""
Write-Host "Build complete: $Exe"
