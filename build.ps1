$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (!(Test-Path -LiteralPath .\assets\engine\studio_peel.exe)) {
    throw 'Build the CPU suffix engine with build_peel.ps1 before packaging.'
}
python -B -m unittest test_ui_startup
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$assets = (Resolve-Path -LiteralPath .\assets).ProviderPath
python -m PyInstaller --noconfirm --clean --onedir --windowed --name SlasherStudio --icon (Join-Path $assets 'studio.ico') --add-data "$assets;assets" --distpath .\.build\dist --workpath .\.build\pyinstaller --specpath .\.build .\studio.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python -B .\package_release.py app
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python -B .\package_release.py checksum
