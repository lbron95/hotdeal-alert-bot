# Run the hotdeal bot once on this PC (Task Scheduler calls this every 15 minutes).
# Token and key live only in the .env file next to this script (git-ignored).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"

Get-Content ".env" -Encoding utf8 | ForEach-Object {
    if ($_.TrimStart([char]0xFEFF) -match '^\s*([A-Z_]+)\s*=\s*(.+?)\s*$') { Set-Item "env:$($Matches[1])" $Matches[2] }
}

New-Item -ItemType Directory -Force logs | Out-Null
$log = "logs\$(Get-Date -Format yyyy-MM).log"
"==== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" | Out-File $log -Append -Encoding utf8
$ErrorActionPreference = "Continue"   # python stderr must not abort the script
python hotdeal.py 2>&1 | ForEach-Object { "$_" } | Out-File $log -Append -Encoding utf8
