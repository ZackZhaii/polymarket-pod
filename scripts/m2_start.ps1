<#
Issue #133 (Task BR, Poly sentinel M2 runbook G-P0-6): start M2 on NoisePC, paper only.

    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\m2_start.ps1 -Reason boot -M2End 2026-10-20T00:00:00Z
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\m2_start.ps1 -Reason reboot            # later starts
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\m2_start.ps1 -Reason check -DryRun     # plan only

A thin wrapper: everything (order, checks, run dir, chain line) is python -m src.m2_start in this
checkout, whose exit code it returns. The resident Sentinel starts first and nothing else starts
until its own fresh heartbeat is verified; see src/m2_start.py and docs/PHASE0_RUN.md.

This wrapper reads no .env or credential, passes no live flag, never resumes a halt and never
registers a scheduled task or service (boot start is a manual Operator step, docs/PHASE0_RUN.md).
src.m2_start refuses to run from the M1 clone.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Reason,
    [string]$M2End = '',
    [switch]$DryRun,
    [string]$Python = ''
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $Python) {
    $venv = Join-Path $root '.venv\Scripts\python.exe'
    if (Test-Path $venv) { $Python = $venv } else { $Python = 'python' }
}
$arguments = @('-m', 'src.m2_start', '--reason', $Reason)
if ($M2End) { $arguments += @('--m2-end', $M2End) }
if ($DryRun) { $arguments += '--dry-run' }
Push-Location $root
try {
    & $Python @arguments
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $code
