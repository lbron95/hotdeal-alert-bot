# Register the hotdeal bot in Windows Task Scheduler (every 15 minutes, no window).
# Usage (in this folder):  powershell -ExecutionPolicy Bypass -File setup_pc.ps1
Set-Location $PSScriptRoot

if (-not (Test-Path ".env")) {
    "TELEGRAM_BOT_TOKEN=paste_bot_token_here`r`nGEMINI_API_KEY=paste_gemini_key_here" | Out-File ".env" -Encoding utf8
    notepad ".env"
    Write-Host "Paste the token and key into .env, save it, then run this script again."
    exit
}

python -m pip install -q -r requirements.txt

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSScriptRoot\run_pc.ps1`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "HotdealBot" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null

Write-Host "Registered: runs every 15 minutes. Logs go to the logs folder."
Write-Host "To remove: Unregister-ScheduledTask -TaskName HotdealBot -Confirm:`$false"
