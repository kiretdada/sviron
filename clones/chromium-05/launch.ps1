$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $Root
& python .\tools\run_clone.py --clone "$PSScriptRoot"
