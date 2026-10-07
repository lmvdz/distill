[CmdletBinding()]
param(
    [ValidateRange(1, 5000)][int]$DailyCap = 50,
    [ValidateSet('codex', 'claude', 'omniroute')][string]$Provider = 'codex',
    [ValidatePattern('^[A-Za-z0-9:/_.-]+$')][string]$Model = 'gpt-6.1-sol',
    [switch]$OwnVideoWorker
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$distillPath = Join-Path $projectRoot '.venv-search/Scripts/distill.exe'
if (-not (Test-Path -LiteralPath $distillPath)) {
    $distillPath = Join-Path $projectRoot '.venv/Scripts/distill.exe'
}
if (-not (Test-Path -LiteralPath $distillPath)) {
    throw 'Install the papers and search extras with uv sync --extra papers --extra search first.'
}

Push-Location -LiteralPath $projectRoot
try {
    $report = (& $distillPath autopilot --report | Out-String | ConvertFrom-Json)
    if ($report.service.status -eq 'running') {
        $serviceProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($report.service.pid)"
        if ($serviceProcess.CommandLine -match 'autopilot') {
            Write-Output "Autopilot is already running (PID $($report.service.pid))."
            return
        }
    }
    $dataPath = Join-Path $projectRoot 'data'
    New-Item -ItemType Directory -Path $dataPath -Force | Out-Null
    $logStem = Join-Path $dataPath ('autopilot-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    $arguments = @('autopilot', '--daily-cap', $DailyCap, '--provider', $Provider, '--model', $Model)
    $rootPattern = [regex]::Escape($projectRoot)
    $videoWorker = Get-CimInstance Win32_Process -Filter "Name='distill.exe'" |
        Where-Object { $_.CommandLine -match $rootPattern -and $_.CommandLine -match '\brun\b' }
    if ($videoWorker -and -not $OwnVideoWorker) {
        $arguments += '--external-video-worker'
    }
    $env:HF_HUB_DISABLE_XET = '1'
    $process = Start-Process -FilePath $distillPath -ArgumentList $arguments -WorkingDirectory $projectRoot `
        -WindowStyle Hidden -RedirectStandardOutput ($logStem + '.out.log') `
        -RedirectStandardError ($logStem + '.err.log') -PassThru
    Write-Output "Started autopilot (launcher PID $($process.Id)); research cap $DailyCap/day."
    Write-Output "Logs: $logStem.out.log and $logStem.err.log"
    Write-Output 'Use uv run --no-sync distill autopilot --report for progress.'
    Write-Output 'Use uv run --no-sync distill autopilot --stop to stop.'
}
finally {
    Pop-Location
}
