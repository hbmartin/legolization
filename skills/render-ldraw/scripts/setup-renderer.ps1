# Detect or install an LDraw renderer on Windows.
#
# -Check only reports what is available (renderer=<name|NONE>); the
# conversational skill asks the user before running the install mode.
# Windows policy: LeoCAD via winget.
[CmdletBinding()]
param(
    [switch]$Check
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Find-Renderer {
    if ($env:LEGOLIZATION_RENDERER -and $env:LEGOLIZATION_RENDERER -ne 'none') {
        return $env:LEGOLIZATION_RENDERER
    }
    foreach ($name in @('ldview', 'leocad')) {
        if (Get-Command $name -ErrorAction SilentlyContinue) { return $name }
    }
    $candidates = @(
        "$env:ProgramFiles\LeoCAD\leocad.exe",
        "${env:ProgramFiles(x86)}\LeoCAD\leocad.exe",
        "$env:ProgramFiles\LDView\LDView64.exe",
        "$env:ProgramFiles\LDView\LDView.exe"
    )
    foreach ($path in $candidates) {
        if ($path -and (Test-Path $path)) {
            if ($path -match 'LDView') { return 'ldview' }
            return 'leocad'
        }
    }
    return $null
}

$renderer = Find-Renderer
if ($Check) {
    if ($renderer) {
        Write-Output "renderer=$renderer"
        exit 0
    }
    Write-Output 'renderer=NONE'
    exit 1
}

if ($renderer) {
    Write-Output "renderer=$renderer (already installed)"
    exit 0
}

if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Error 'winget is required to install LeoCAD; install it from the Microsoft Store'
    exit 1
}

winget install --exact --id LeonardoZide.LeoCAD `
    --accept-package-agreements --accept-source-agreements

$renderer = Find-Renderer
if ($renderer) {
    Write-Output "renderer=$renderer"
    exit 0
}
Write-Error 'renderer installation completed but no renderer was detected'
exit 1
