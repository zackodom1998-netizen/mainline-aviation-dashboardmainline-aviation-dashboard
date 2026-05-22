# register_task.ps1
# Registers a Windows Task Scheduler job that runs the Springshot dashboard
# refresh every day at 1:00 AM.  Run this once (setup.bat calls it for you).

$TaskName   = "Goldbergs - Springshot Dashboard Refresh"
$ScriptPath = 'C:\Users\Tony Quach\OneDrive - Goldbergs Group\Desktop\TEST\automation\springshot_full_refresh.py'
$BatPath    = 'C:\Users\Tony Quach\OneDrive - Goldbergs Group\Desktop\TEST\automation\refresh_now.bat'

# Find Python executable
$PythonExe = $null
try { $PythonExe = (Get-Command python  -ErrorAction Stop).Source } catch {}
if (-not $PythonExe) {
    try { $PythonExe = (Get-Command py -ErrorAction Stop).Source } catch {}
}
if (-not $PythonExe) {
    # Common install locations
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
        "C:\Python312\python.exe",
        "C:\Python311\python.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $PythonExe = $c; break }
    }
}

if (-not $PythonExe) {
    Write-Warning "Python not found — scheduling the .bat launcher instead (it will locate Python at runtime)."
    $Action = New-ScheduledTaskAction -Execute $BatPath
} else {
    Write-Host "Using Python: $PythonExe"
    $Action = New-ScheduledTaskAction -Execute $PythonExe -Argument "`"$ScriptPath`""
}

$Trigger  = New-ScheduledTaskTrigger -Daily -At "01:00AM"
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew

try {
    Register-ScheduledTask `
        -TaskName    $TaskName `
        -Action      $Action `
        -Trigger     $Trigger `
        -Settings    $Settings `
        -Description "Daily refresh of Goldbergs ATL Missions Dashboard from Springshot API" `
        -RunLevel    Limited `
        -Force | Out-Null

    Write-Host ""
    Write-Host "Scheduled task registered successfully:"
    Write-Host "  Name  : $TaskName"
    Write-Host "  Runs  : Every day at 1:00 AM"
    Write-Host "  Action: $PythonExe `"$ScriptPath`""
    Write-Host ""
} catch {
    Write-Error "Failed to register task: $_"
    exit 1
}
