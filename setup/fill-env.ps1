# Makes the passwords and keys in .env for start.bat, the same way SETUP-WINDOWS.cmd does. Written for PowerShell 5.1.
# What is already in .env always wins: Postgres keeps its first password in the Docker volume, so a new one would lock
# you out of your own data. Only names that are missing or blank get filled in, and everything else is left alone.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root '.env'
$stateDir = Join-Path $env:LOCALAPPDATA 'HarborlineLab'
$backup = Join-Path $stateDir 'env.backup'
$keyNames = @('POSTGRES_PASSWORD', 'HL_APP_PASSWORD', 'HL_ETL_PASSWORD', 'HL_REVIEWER_PASSWORD', 'SF_CLIENT_ID', 'SF_CLIENT_SECRET', 'CARRIER_API_KEY')
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null

function Get-Value([string]$Path, [string]$Key) {
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    foreach ($line in [IO.File]::ReadAllLines($Path)) {
        if ($line -match "^\s*$([regex]::Escape($Key))\s*=\s*(.*?)\s*$") { return $Matches[1] }
    }
    return ''
}
function Set-Value([string]$Key, [string]$Value) {
    $lines = New-Object System.Collections.ArrayList
    $found = $false
    foreach ($line in [IO.File]::ReadAllLines($envFile)) {
        if ($line -match "^\s*$([regex]::Escape($Key))\s*=") { [void]$lines.Add("$Key=$Value"); $found = $true } else { [void]$lines.Add($line) }
    }
    if (-not $found) { [void]$lines.Add("$Key=$Value") }
    # No byte-order mark: Docker Compose would read it as part of the first setting's name.
    [IO.File]::WriteAllLines($envFile, [string[]]$lines, (New-Object Text.UTF8Encoding($false)))
}
function New-Key {
    $bytes = New-Object byte[] 24
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return (-join ($bytes | ForEach-Object { $_.ToString('x2') }))
}
function Stop-IfDataWouldLockOut {
    $volume = 'harborline-lab_pgdata'
    $found = @(& docker volume ls -q --filter "name=$volume" | Where-Object { "$_".Trim() -eq $volume })
    if ($found.Count -eq 0) { return }
    Write-Host 'Your settings file (.env) and its backup are both gone, but your simulator data is still here in Docker.'
    Write-Host 'The data is locked with the password that was in that file. A new password would not open it, so this stopped here.'
    Write-Host 'Nothing has been changed or deleted. You have two choices:'
    Write-Host '  1  Keep your data: put your old .env back in this folder, from a backup or another copy, then run start.bat again.'
    Write-Host '  2  Start fresh: this deletes all your simulator data and tracker progress. Run these, then run start.bat again:'
    Write-Host '       docker rm -f harborline-lab-app-1 harborline-lab-postgres-1'
    Write-Host '       docker volume rm harborline-lab_pgdata'
    exit 1
}

if (-not (Test-Path -LiteralPath $envFile)) {
    if (Test-Path -LiteralPath $backup) {
        Copy-Item -LiteralPath $backup -Destination $envFile
        Write-Host 'Your .env was missing, so it was restored from its backup. Your passwords still match your data.'
    } else {
        Stop-IfDataWouldLockOut
        Copy-Item -LiteralPath (Join-Path $root '.env.example') -Destination $envFile
        Write-Host 'Created .env with passwords and keys made just for this computer.'
    }
}
foreach ($key in $keyNames) {
    if (Get-Value $envFile $key) { continue }
    $value = Get-Value $backup $key
    if (-not $value) {
        if ($key -eq 'POSTGRES_PASSWORD') { Stop-IfDataWouldLockOut }
        $value = New-Key
    }
    Set-Value $key $value
}
Copy-Item -LiteralPath $envFile -Destination $backup -Force
exit 0
