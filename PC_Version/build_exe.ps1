$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m PyInstaller --noconfirm --clean --onefile --windowed --name ArcFlashPC --icon "$PSScriptRoot\ArcFlashPC.ico" --hidden-import webview --add-data "$PSScriptRoot\static;static" --distpath dist --workpath build\work --specpath build app.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Output "Built: $PSScriptRoot\dist\ArcFlashPC.exe"
