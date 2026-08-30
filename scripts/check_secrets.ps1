$ErrorActionPreference = 'Stop'
$patterns = 'sk-[A-Za-z0-9_-]{10,}','Bearer\s+[A-Za-z0-9._-]{20,}'
$matches = rg --hidden -n -g '!.git/**' -g '!graphify-out/**' -g '!**/__pycache__/**' ($patterns -join '|') . 2>$null
if ($LASTEXITCODE -eq 0 -and $matches) { Write-Error "Potential secret detected. Review matches before publishing.`n$matches" }
Write-Host 'Secret scan passed.'
