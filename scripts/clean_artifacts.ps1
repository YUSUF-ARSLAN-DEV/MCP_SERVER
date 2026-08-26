$ErrorActionPreference = 'Stop'
$paths = @('test-results','playwright-report','reports','exploration','traces','evidence.json','results.json','generation-debug.log')
foreach ($path in $paths) {
  if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
}
Write-Host 'Generated test artifacts removed.'
