# Build the Windows installer: a self-contained Athens.exe packaged into
# Athens-Setup-<version>.exe (Inno Setup), or a portable .zip if Inno Setup
# isn't installed. Run this ON Windows (PyInstaller can't cross-compile — the
# macOS build is scripts/build-macos.sh).
#
#   pip install -e ".[ui,midi,osc,package]"
#   pip install pillow                     # only if packaging\Athens.ico is missing
#   powershell -ExecutionPolicy Bypass -File scripts\build-windows.ps1
#
# Results:
#   dist\Athens\Athens.exe                 (self-contained app folder)
#   dist\Athens-Setup-<version>.exe        (the installer, if Inno Setup present)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$version = (Select-String -Path pyproject.toml -Pattern '^version\s*=\s*"([^"]*)"').Matches[0].Groups[1].Value
Write-Host "== Athens $version - Windows build =="

# Probe for a python module without tripping ErrorActionPreference=Stop, which
# turns a native command's redirected stderr into a script-killing ErrorRecord.
function Test-PyModule($python, $module) {
    $eap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $python -c "import $module" 2>$null
    $ok = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $eap
    return $ok
}

# the interpreter that has PyInstaller: prefer $env:PYTHON, then .venv, then PATH
if ($env:PYTHON) { $py = $env:PYTHON }
elseif (Test-Path .venv\Scripts\python.exe) { $py = ".venv\Scripts\python.exe" }
else { $py = "python" }
if (-not (Test-PyModule $py "PyInstaller")) {
    Write-Host "error: PyInstaller not found in '$py'. Run: pip install -e `".[ui,midi,osc,package]`""
    exit 1
}

# A Windows .exe needs a .ico. Generate one from the iconset PNG (Pillow) if the
# committed packaging\Athens.ico is missing.
if (-not (Test-Path packaging\Athens.ico)) {
    Write-Host "== generating packaging\Athens.ico from the iconset =="
    if (-not (Test-PyModule $py "PIL")) { & $py -m pip install pillow }
    & $py -c "from PIL import Image; Image.open('packaging/Athens.iconset/icon_512x512.png').save('packaging/Athens.ico', sizes=[(256,256),(128,128),(64,64),(48,48),(32,32),(16,16)])"
}

& $py -m PyInstaller --noconfirm --clean Athens.spec

$iscc = Get-Command iscc -ErrorAction SilentlyContinue
if ($iscc) {
    Write-Host "== packaging installer via Inno Setup =="
    & $iscc.Source "/DMyAppVersion=$version" packaging\Athens.iss
    Write-Host ""
    Write-Host "installer: dist\Athens-Setup-$version.exe"
} else {
    $zip = "dist\Athens-$version-win.zip"
    if (Test-Path $zip) { Remove-Item $zip }
    Compress-Archive -Path dist\Athens\* -DestinationPath $zip
    Write-Host ""
    Write-Host "Inno Setup (iscc) not found - shipped a portable zip: $zip"
    Write-Host "Install Inno Setup (https://jrsoftware.org/isdl.php) for a real installer."
}
Write-Host "built: dist\Athens\Athens.exe"
