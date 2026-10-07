[CmdletBinding()]
param(
    [ValidateRange(1, 10000)][int]$DailyCap = 200,
    [ValidateSet('codex', 'claude', 'omniroute')][string]$Provider = 'codex',
    [ValidatePattern('^[A-Za-z0-9:/_.-]+$')][string]$Model = 'gpt-6.1-sol'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$distillPath = Join-Path $projectRoot '.venv/Scripts/distill.exe'
if (-not (Test-Path -LiteralPath $distillPath)) {
    throw 'Run uv sync before starting Distill maintenance.'
}
$rootPattern = [regex]::Escape($projectRoot)
$active = Get-CimInstance Win32_Process -Filter "Name='distill.exe'" |
    Where-Object { $_.CommandLine -match $rootPattern -and $_.CommandLine -match '\bknowledge\b.*\bmaintain\b' }
if ($active) {
    Write-Output "Distill maintenance is already running (PID $($active.ProcessId -join ', '))."
    return
}
$dataPath = if ($env:DISTILL_DATA) { $env:DISTILL_DATA } else { Join-Path $projectRoot 'data' }
if (-not [System.IO.Path]::IsPathRooted($dataPath)) {
    $dataPath = Join-Path $projectRoot $dataPath
}
New-Item -ItemType Directory -Path $dataPath -Force | Out-Null
$logStem = Join-Path $dataPath ('distill-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
$arguments = @('knowledge', '--provider', $Provider, '--model', $Model, 'maintain', '--daily-cap', $DailyCap)
$process = Start-Process -FilePath $distillPath -ArgumentList $arguments -WorkingDirectory $projectRoot `
    -WindowStyle Hidden -RedirectStandardOutput ($logStem + '.out.log') `
    -RedirectStandardError ($logStem + '.err.log') -PassThru
Write-Output "Started Distill maintenance (launcher PID $($process.Id)); stage-request cap $DailyCap/day."
Write-Output "Logs: $logStem.out.log and $logStem.err.log"
Write-Output 'Use uv run --no-sync distill knowledge status for progress.'
Write-Output 'Use uv run --no-sync distill knowledge maintain --stop to stop.'
