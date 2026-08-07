# Install or update the managed official LDraw parts library.
#
# Thin wrapper: the download/validation/metadata/weekly logic lives in
# `legolization parts sync` so shell and PowerShell stay in lockstep.
# Silent and admin-free; a valid existing library survives a failed
# weekly check. Pass -Force to re-download regardless of freshness.
[CmdletBinding()]
param(
    [switch]$Force
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not (Get-Command legolization -ErrorAction SilentlyContinue)) {
    Write-Host 'legolization CLI not found; installing via uvx.sh...'
    Invoke-RestMethod https://uvx.sh/legolization/install.ps1 | Invoke-Expression
}

if (-not (Get-Command legolization -ErrorAction SilentlyContinue)) {
    Write-Error 'legolization installation failed; cannot set up the parts library'
    exit 1
}

$syncArgs = @('parts', 'sync')
if ($Force) { $syncArgs += '--force' }
& legolization @syncArgs
exit $LASTEXITCODE
