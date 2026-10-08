param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& $Python scripts\build_release.py
if ($LASTEXITCODE -ne 0) { throw 'Build failed; review build/checks and build/pyinstaller.txt.' }
