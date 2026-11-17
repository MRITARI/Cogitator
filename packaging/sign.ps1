<#
  Authenticode-signs the given files with signtool, when a code-signing certificate is configured.
  Secrets (environment): CODESIGN_PFX_BASE64 (the .pfx, base64) and CODESIGN_PFX_PASSWORD.
  Without CODESIGN_PFX_BASE64 it does nothing and exits 0, so unsigned releases keep working.
      powershell -File packaging\sign.ps1 -Files a.exe,b.exe
#>
param([Parameter(Mandatory = $true)][string[]]$Files)
$ErrorActionPreference = "Stop"
if (-not $env:CODESIGN_PFX_BASE64) { Write-Host "No code-signing certificate configured; skipping."; exit 0 }
$signtool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending | Select-Object -First 1
if (-not $signtool) { throw "signtool.exe not found (install the Windows SDK)" }
$tmp = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { $env:TEMP }
$pfx = Join-Path $tmp "cogitator-codesign.pfx"
[IO.File]::WriteAllBytes($pfx, [Convert]::FromBase64String($env:CODESIGN_PFX_BASE64))
try {
    foreach ($f in $Files) {
        & $signtool.FullName sign /f $pfx /p $env:CODESIGN_PFX_PASSWORD /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $f
        if ($LASTEXITCODE -ne 0) { throw "signtool failed for $f" }
    }
} finally {
    Remove-Item $pfx -Force -ErrorAction SilentlyContinue
}
