[CmdletBinding()]
param(
    [switch]$NoLaunch
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'build_windows.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Windows build failed.' }

$dist = Join-Path $projectRoot 'dist'
$names = @('funpay-operations.exe', 'funpay-operations-cli.exe', 'funpay-operations-setup.exe', 'funpay-operations-auth.exe', 'THIRD_PARTY_NOTICES.md')
foreach ($name in $names) {
    $file = Join-Path $dist $name
    if (-not (Test-Path -LiteralPath $file) -or (Get-Item -LiteralPath $file).Length -le 0) {
        throw "Expected non-empty build output is missing: $name"
    }
}

$cli = Join-Path $dist 'funpay-operations-cli.exe'
& $cli setup --non-interactive
if ($LASTEXITCODE -ne 0) { throw 'Packaged first-run installation failed.' }

$application = Join-Path $env:LOCALAPPDATA 'FunPay Operations\app'
foreach ($name in $names) {
    $file = Join-Path $application $name
    if (-not (Test-Path -LiteralPath $file) -or (Get-Item -LiteralPath $file).Length -le 0) {
        throw "Expected installed executable is missing: $name"
    }
}

$installedCli = Join-Path $application 'funpay-operations-cli.exe'
$installedBackground = Join-Path $application 'funpay-operations.exe'
$installedSetup = Join-Path $application 'funpay-operations-setup.exe'
$installedAuth = Join-Path $application 'funpay-operations-auth.exe'
& $installedCli diagnostics
if ($LASTEXITCODE -ne 0) { throw 'Installed diagnostics failed.' }
& $installedBackground --background --once
if ($LASTEXITCODE -ne 0) { throw 'Installed background smoke cycle failed.' }
& $installedSetup --smoke
if ($LASTEXITCODE -ne 0) { throw 'Installed Setup Center smoke test failed.' }
& $installedSetup --gui-runtime-smoke
if ($LASTEXITCODE -ne 0) { throw 'Installed Setup Center GUI runtime smoke test failed.' }
& $installedAuth --runtime-status
if ($LASTEXITCODE -ne 0) { throw 'Installed WebView2 Runtime check failed.' }

[xml]$task = schtasks /Query /TN 'FunPay Operations Background' /XML
$taskTarget = $task.Task.Actions.Exec.Command
if ($taskTarget -ne $installedBackground) {
    throw 'Task Scheduler target does not match the installed background executable.'
}

$shortcut = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\FunPay Operations Setup.lnk'
if (-not (Test-Path -LiteralPath $shortcut)) { throw 'Start Menu shortcut is missing.' }
$shell = New-Object -ComObject WScript.Shell
if ($shell.CreateShortcut($shortcut).TargetPath -ne $installedSetup) {
    throw 'Start Menu shortcut target does not match the installed Setup Center.'
}

Get-ChildItem -LiteralPath $application | Where-Object { $_.Name -like 'funpay-operations*.exe' -or $_.Name -eq 'THIRD_PARTY_NOTICES.md' } | Select-Object Name, Length
if (-not $NoLaunch) {
    Start-Process -FilePath $installedSetup
}
} finally {
    Pop-Location
}
