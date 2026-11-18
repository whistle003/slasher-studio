param(
    [string]$Upstream = (Join-Path $PSScriptRoot '..\hash-slinging-slasher'),
    [string]$Rustc = 'rustc'
)
$ErrorActionPreference = 'Stop'
$upstreamPath = (Resolve-Path -LiteralPath $Upstream).ProviderPath
$obj = Join-Path $PSScriptRoot '.build\peel'
New-Item -ItemType Directory -Force -Path $obj | Out-Null
$lib = Join-Path $obj 'libslasher.rlib'
$env:CARGO_MANIFEST_DIR = $upstreamPath
& $Rustc --edition 2021 --crate-name slasher --crate-type rlib -C opt-level=3 (Join-Path $upstreamPath 'src\lib.rs') -o $lib
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $Rustc --edition 2021 -C opt-level=3 --extern "slasher=$lib" (Join-Path $PSScriptRoot 'studio_peel.rs') -o (Join-Path $PSScriptRoot 'assets\engine\studio_peel.exe')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
