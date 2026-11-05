<# : Harborline Lab setup for Windows. Double-click this file. The batch part below only starts PowerShell.
@echo off
setlocal
title Harborline Lab setup
set "HL_SETUP_FILE=%~f0"
set "HL_SETUP_DIR=%~dp0"
powershell.exe -NoLogo -NoProfile -Command "if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') { exit 7 }"
if "%ERRORLEVEL%"=="7" (
  echo.
  echo This computer only allows PowerShell in a restricted mode, usually because of a company policy.
  echo Setup cannot run here until whoever manages this computer allows it. Show them this message.
  echo.
  if not "%HL_SETUP_NOPAUSE%"=="1" pause
  exit /b 1
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$s = [IO.File]::ReadAllText($env:HL_SETUP_FILE); & ([ScriptBlock]::Create($s))"
set "HL_RC=%ERRORLEVEL%"
rem 75: setup carried on in a fresh window, so this one can just close.
if "%HL_RC%"=="75" exit /b 0
echo.
if not "%HL_SETUP_NOPAUSE%"=="1" pause
exit /b %HL_RC%
: end of the batch part #>

# Everything below is PowerShell 5.1 (it ships with Windows). Every step checks the machine first and skips what is
# already done, so running this file again is always safe and carries on where it stopped.
# Keep this file ASCII with CRLF line endings: cmd.exe reads the top part line by line.

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false) } catch { }
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }
# Leftovers from other tools that would silently point this run at the wrong account or Docker engine.
Remove-Item Env:GH_TOKEN, Env:GITHUB_TOKEN, Env:DOCKER_HOST, Env:DOCKER_TLS_VERIFY, Env:DOCKER_CERT_PATH, Env:DOCKER_CONTEXT -ErrorAction SilentlyContinue
$env:WSL_UTF8 = '1'
# The update check goes to the internet, so it needs the real proxy: keep it before the next line turns it off.
$Script:SystemProxy = $null
try {
    $Script:SystemProxy = [System.Net.WebRequest]::GetSystemWebProxy()
    $Script:SystemProxy.Credentials = [System.Net.CredentialCache]::DefaultCredentials
} catch { }
# Never send calls to this machine's own app through a system proxy.
try { [System.Net.WebRequest]::DefaultWebProxy = New-Object System.Net.WebProxy } catch { }

$Script:SourceDir = $env:HL_SETUP_DIR.TrimEnd('\', '/')
$Script:Root = if ($env:HL_LAB_ROOT) { $env:HL_LAB_ROOT } else { 'C:\dev' }
$Script:LabName = 'harborline-lab'
$Script:RepoName = 'harborline-analytics'
$Script:LabDir = Join-Path $Script:Root $Script:LabName
$Script:RepoDir = Join-Path $Script:Root $Script:RepoName
$Script:OldProject = 'harborline'
$Script:Project = 'harborline-lab'
$Script:OldFolders = @('harborline-simulator', 'harborline-3pl-simulator', 'harborline-data-platform')
$Script:StateDir = Join-Path $env:LOCALAPPDATA 'HarborlineLab'
$Script:LogFile = Join-Path $Script:StateDir 'setup.log'
$Script:ErrorFile = Join-Path $Script:StateDir 'setup-error.txt'
$Script:UpdateDir = Join-Path $Script:StateDir 'update'
$Script:UpdatedToFile = Join-Path $Script:StateDir 'updated-to.txt'
$Script:FileList = Join-Path $Script:StateDir 'installed-files.txt'
$Script:EnvBackup = Join-Path $Script:StateDir 'env.backup'
$Script:KeyNames = @('POSTGRES_PASSWORD', 'HL_APP_PASSWORD', 'HL_ETL_PASSWORD', 'HL_REVIEWER_PASSWORD', 'SF_CLIENT_ID', 'SF_CLIENT_SECRET', 'CARRIER_API_KEY')
$Script:OwnedFolders = @('.github', 'answer_key', 'brand', 'config', 'db', 'docs', 'handbook', 'setup', 'simulator', 'team', 'tracker', 'web')
$Script:StepNo = 0
$Script:StepTotal = 24
$Script:Results = New-Object System.Collections.ArrayList
$Script:LastOutput = ''
$Script:LastError = ''
$Script:LastCommand = ''
$Script:LastCode = 0
$Script:PassText = ''
$Script:InstalledSomething = $false

New-Item -ItemType Directory -Force -Path $Script:StateDir | Out-Null

# ---------------------------------------------------------------------------------------------- the window
# A click in a classic console window starts a text selection, and that freezes whatever is writing to the window until a
# key is pressed. So clicks only select while setup is waiting for an answer, when nothing is running.
# Kept as text because the administrator window, a console of its own, needs the same thing.
$Script:ClickGuard = @'
function Set-ClickSelects([bool]$On) {
    try {
        if (-not ('HarborlineLab.ConsoleMode' -as [type])) {
            $api = '[DllImport("kernel32.dll")] public static extern System.IntPtr GetStdHandle(int handle);
[DllImport("kernel32.dll")] public static extern bool GetConsoleMode(System.IntPtr handle, out uint mode);
[DllImport("kernel32.dll")] public static extern bool SetConsoleMode(System.IntPtr handle, uint mode);'
            Add-Type -Namespace HarborlineLab -Name ConsoleMode -MemberDefinition $api -ErrorAction Stop
        }
        $stdin = [HarborlineLab.ConsoleMode]::GetStdHandle(-10)
        [uint32]$mode = 0
        if (-not [HarborlineLab.ConsoleMode]::GetConsoleMode($stdin, [ref]$mode)) { return }
        $quickEdit = 0x40
        $extendedFlags = 0x80
        $mode = $mode -bor $extendedFlags
        if ($On) { $mode = $mode -bor $quickEdit } elseif ($mode -band $quickEdit) { $mode = $mode - $quickEdit }
        [void][HarborlineLab.ConsoleMode]::SetConsoleMode($stdin, $mode)
    } catch { }
}
'@
. ([ScriptBlock]::Create($Script:ClickGuard))
Set-ClickSelects $false

# ---------------------------------------------------------------------------------------------- output
function Write-Log([string]$Text) {
    try { Add-Content -LiteralPath $Script:LogFile -Value ("{0}  {1}" -f (Get-Date -Format 's'), $Text) -Encoding UTF8 } catch { }
}
function Say([string]$Text) { Write-Host "    $Text"; Write-Log $Text }
function Say-Good([string]$Text) { Write-Host "    $Text" -ForegroundColor Green; Write-Log $Text }
function Say-Warn([string]$Text) { Write-Host "    $Text" -ForegroundColor Yellow; Write-Log "WARN $Text" }

function Your-Turn {
    param([string[]]$Lines, [switch]$NoWait)
    Write-Host ''
    Write-Host '    >>> YOUR TURN' -ForegroundColor Yellow
    foreach ($l in $Lines) { Write-Host "    $l" -ForegroundColor Yellow; Write-Log "MANUAL $l" }
    if (-not $NoWait) { Read-Answer '    Press Enter when that is done' | Out-Null }
    Write-Host ''
}
function Read-Answer([string]$Label) {
    # Clicks select again here, so right-click paste and copying a code from the window still work.
    Set-ClickSelects $true
    $answer = Read-Host $Label
    Set-ClickSelects $false
    return $answer
}
function Ask-Text([string]$Prompt, [string]$Default = '') {
    $label = if ($Default) { "    $Prompt [$Default]" } else { "    $Prompt" }
    $answer = Read-Answer $label
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return $answer.Trim()
}
function Ask-YesNo([string]$Prompt, [bool]$Default = $true) {
    $hint = if ($Default) { 'Y/n' } else { 'y/N' }
    $answer = Read-Answer "    $Prompt ($hint)"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return ($answer.Trim() -match '^(y|yes)$')
}

# ---------------------------------------------------------------------------------------------- running things
function Invoke-Tool {
    # Runs a program, keeps its output for error reports, returns $true when it exits with 0.
    param([string]$Exe, [string[]]$ArgList = @(), [switch]$Live)
    $Script:LastCommand = ("$Exe " + ($ArgList -join ' ')).Trim()
    Write-Log "RUN $($Script:LastCommand)"
    # Again each time, in case the last program changed the window's mode.
    Set-ClickSelects $false
    $lines = New-Object System.Collections.ArrayList
    $global:LASTEXITCODE = 0
    $code = 0
    try {
        & $Exe @ArgList 2>&1 | ForEach-Object {
            $line = ("$_" -replace "`0", '').TrimEnd()
            if ($line) {
                [void]$lines.Add($line)
                Write-Log "  $line"
                if ($Live -and $line -match '[A-Za-z]{3}' -and $line -notmatch '^\s*[-\\|/]\s*$') { Write-Host "      $line" -ForegroundColor DarkGray }
            }
        }
        $code = $global:LASTEXITCODE
    } catch {
        [void]$lines.Add($_.Exception.Message)
        $code = 9009
    }
    $Script:LastCode = $code
    $Script:LastOutput = ($lines -join "`n")
    return ($code -eq 0)
}
function Invoke-Interactive([string]$Exe, [string[]]$ArgList) {
    # For programs that talk to the user. Start-Process keeps them on the real console whatever the caller pipes to.
    $Script:LastCommand = "$Exe $($ArgList -join ' ')"
    Write-Log "RUN (interactive) $($Script:LastCommand)"
    # Not -Wait: that also waits for anything it started, such as the browser, and looks frozen.
    Set-ClickSelects $true
    $p = Start-Process -FilePath (Get-Command $Exe).Source -ArgumentList $ArgList -NoNewWindow -PassThru
    $null = $p.Handle
    $p.WaitForExit()
    Set-ClickSelects $false
    $Script:LastCode = $p.ExitCode
    return ($p.ExitCode -eq 0)
}
function Invoke-Elevated([string]$Command, [string]$Why) {
    Say "Windows will ask for permission ($Why). Click Yes."
    Write-Log "ELEVATED $Command"
    # The elevated window can't report back, so it writes its output to a file for the error report.
    $out = Join-Path $Script:StateDir 'elevated-output.txt'
    Remove-Item -LiteralPath $out -ErrorAction SilentlyContinue
    $wrapped = "$($Script:ClickGuard)`nSet-ClickSelects `$false`n& { $Command } *>&1 | Tee-Object -FilePath '" + ($out -replace "'", "''") + "'; exit `$LASTEXITCODE"
    $bytes = [Text.Encoding]::Unicode.GetBytes($wrapped)
    try {
        $p = Start-Process powershell.exe -Verb RunAs -Wait -PassThru -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', [Convert]::ToBase64String($bytes))
        $Script:LastCommand = "(as administrator) $Command"
        $Script:LastCode = $p.ExitCode
        $Script:LastOutput = "Exit code $($p.ExitCode)"
        if (Test-Path -LiteralPath $out) { $Script:LastOutput = (Get-Content -LiteralPath $out -Raw) + "`n" + $Script:LastOutput }
        return ($p.ExitCode -eq 0)
    } catch {
        $Script:LastCommand = "(as administrator) $Command"
        $Script:LastOutput = "Administrator permission was not given: $($_.Exception.Message)"
        return $false
    }
}
function Update-Path {
    # Installers change PATH for new windows only; this window needs it now.
    $parts = @(
        [Environment]::GetEnvironmentVariable('Path', 'Machine'),
        [Environment]::GetEnvironmentVariable('Path', 'User'),
        (Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin'),
        (Join-Path $env:ProgramFiles 'Git\cmd'),
        (Join-Path $env:ProgramFiles 'GitHub CLI'),
        (Join-Path $env:ProgramFiles 'Microsoft VS Code\bin'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Microsoft VS Code\bin'),
        (Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Git\cmd'),
        (Join-Path $env:LOCALAPPDATA 'Programs\GitHub CLI'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links'),
        (Join-Path $env:USERPROFILE '.local\bin'),
        (Join-Path $env:SystemRoot 'Sysnative')
    )
    $env:Path = ($parts | Where-Object { $_ }) -join ';'
}
function Restart-InFreshWindow {
    # A terminal only learns about newly installed programs when it opens. Update-Path covers nearly all of that, but
    # when a step fails right after an install, one fresh window (started by Explorer, not by this one) is worth trying.
    if (-not $Script:InstalledSomething) { return }
    $marker = Join-Path $Script:StateDir 'fresh-window.done'
    if ((Test-Path -LiteralPath $marker) -and (Get-Item -LiteralPath $marker).LastWriteTime -gt (Get-Date).AddHours(-1)) { return }
    Set-Content -LiteralPath $marker -Value (Get-Date -Format 's') -Encoding Ascii
    $file = Join-Path $Script:LabDir 'SETUP-WINDOWS.cmd'
    # Only the installed copy if it is this version: an older one would undo what this run is here to install.
    if (-not (Test-Path -LiteralPath $file) -or (Get-VersionIn $Script:LabDir) -ne (Get-VersionIn $Script:SourceDir)) { $file = $env:HL_SETUP_FILE }
    Say 'Something was just installed, and a new window often sees it when this one cannot. Carrying on in a fresh window.'
    try { Start-Process explorer.exe -ArgumentList ('"' + $file + '"') | Out-Null } catch { return }
    exit 75
}
function Test-Command([string]$Name) { return [bool](Get-Command $Name -ErrorAction SilentlyContinue) }

function Install-Package {
    # Tries each winget id in turn; several exist because ids get renamed (DBeaver did).
    param([string[]]$Ids, [string]$Label)
    Say "Installing $Label (this can take a few minutes; Windows may ask for permission, click Yes)."
    $Script:InstalledSomething = $true
    # By exit code, because winget's messages change with the Windows language: installed, already installed, no upgrade.
    $fine = @(0, -1978335189, -1978335135, -1978334963, -1978334962, -1978334967)
    foreach ($id in $Ids) {
        $flags = @('install', '--id', $id, '-e', '--source', 'winget', '--silent', '--accept-package-agreements', '--accept-source-agreements')
        Invoke-Tool winget $flags -Live | Out-Null
        Update-Path
        if ($fine -contains $Script:LastCode -or $Script:LastOutput -match 'already installed|No available upgrade|No newer package') { return $true }
        if ($Script:LastCode -eq -1978334966) { Request-Restart "$Label needs a restart to finish installing." }
        if ($Script:LastOutput -match 'Installer failed with exit code') {
            Say 'The installer failed as a normal user. Trying again as administrator.'
            if (Invoke-Elevated ("winget " + ($flags -join ' ')) "to install $Label") { Update-Path; return $true }
        }
        if ($Script:LastOutput -notmatch 'No package found') { break }
    }
    # Kept apart from LastOutput so a later command in the same step can't hide why this one failed.
    $Script:LastError = "$Label could not be installed: $($Script:LastOutput)"
    return $false
}

# ---------------------------------------------------------------------------------------------- machine facts
function Get-MachineInfo {
    # The registry for the build: a damaged WMI must not read as "Windows is too old".
    $version = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -ErrorAction SilentlyContinue
    $os = $null; $cs = $null; $cpu = $null
    try {
        $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
        $cs = Get-CimInstance Win32_ComputerSystem -ErrorAction Stop
        $cpu = @(Get-CimInstance Win32_Processor -ErrorAction Stop)[0]
    } catch { Say-Warn "Could not read all the computer's details: $($_.Exception.Message)" }
    $drive = Get-PSDrive -Name ($Script:Root.Substring(0, 1)) -ErrorAction SilentlyContinue
    return [pscustomobject]@{
        Build   = if ($version) { [int]$version.CurrentBuildNumber } elseif ($os) { [int]$os.BuildNumber } else { 0 }
        Caption = if ($os) { $os.Caption } elseif ($version) { $version.ProductName } else { 'Windows' }
        RamGB   = if ($cs) { [math]::Round($cs.TotalPhysicalMemory / 1GB, 1) } else { 16 }
        Cores   = if ($cs) { [int]$cs.NumberOfLogicalProcessors } else { 4 }
        FreeGB  = if ($drive) { [math]::Round($drive.Free / 1GB, 0) } else { 100 }
        Virt    = if ($cs) { [bool]($cs.HypervisorPresent -or $cpu.VirtualizationFirmwareEnabled) } else { $true }
    }
}
function Test-PortFree([int]$Port) {
    if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) { return $false }
    # Windows can reserve a port with nothing listening on it (Hyper-V, WinNAT). Only trying to bind finds that out.
    $listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $Port)
    try { $listener.Start(); $listener.Stop(); return $true } catch { return $false }
}
function Test-DockerGroup { return ((whoami /groups | Out-String) -match 'docker-users') }
function Test-WslReady {
    if (-not (Test-Command 'wsl')) { return $false }
    Invoke-Tool 'wsl' @('--status') | Out-Null
    return ($Script:LastCode -eq 0 -and $Script:LastOutput -notmatch 'not installed|not enabled|wsl\.exe --install|Usage: wsl')
}
function Update-Wsl {
    if (Invoke-Tool 'wsl' @('--update')) { return $true }
    # The Microsoft Store route is blocked or broken on some machines.
    return (Invoke-Tool 'wsl' @('--update', '--web-download'))
}
function Stop-WslIfOnlyDocker {
    # wsl --shutdown stops every Linux distribution on the machine. Never do that to someone else's work.
    Invoke-Tool 'wsl' @('--list', '--running', '--quiet') | Out-Null
    if ($Script:LastCode -ne 0) { return $true }
    $others = @($Script:LastOutput -split "`n" | Where-Object { $_.Trim() -and $_ -notmatch '^\s*docker-desktop' })
    if ($others.Count -gt 0) {
        Say-Warn "Other Linux distributions are running ($($others -join ', ')), so setup did not restart WSL. Restart the computer if Docker stays stuck."
        return $false
    }
    Invoke-Tool 'wsl' @('--shutdown') | Out-Null
    return $true
}

# ---------------------------------------------------------------------------------------------- restart handling
function Request-Restart([string]$Why) {
    Write-Host ''
    Say-Warn "$Why"
    try {
        Set-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce' -Name '!HarborlineLabSetup' -Value ('cmd /c ""' + $env:HL_SETUP_FILE + '""')
        Say 'Setup will start again by itself after you sign back in, and carry on from here.'
    } catch {
        Say 'After you sign back in, run this file again. It carries on from here.'
    }
    if (Ask-YesNo 'Restart the computer now? Save your work first.') { Restart-Computer -Force; exit 0 }
    Say 'Restart when you are ready.'
    exit 0
}

# ---------------------------------------------------------------------------------------------- settings file
function Get-EnvPath { return (Join-Path $Script:LabDir '.env') }
function Get-EnvValue([string]$Key, [string]$Default = '') {
    $path = Get-EnvPath
    if (Test-Path -LiteralPath $path) {
        foreach ($line in [IO.File]::ReadAllLines($path)) {
            if ($line -match "^\s*$([regex]::Escape($Key))\s*=\s*(.*?)\s*$") { return $Matches[1] }
        }
    }
    return $Default
}
function Set-EnvValue([string]$Key, [string]$Value) {
    $path = Get-EnvPath
    $lines = New-Object System.Collections.ArrayList
    $found = $false
    foreach ($line in [IO.File]::ReadAllLines($path)) {
        if ($line -match "^\s*$([regex]::Escape($Key))\s*=") { [void]$lines.Add("$Key=$Value"); $found = $true } else { [void]$lines.Add($line) }
    }
    if (-not $found) { [void]$lines.Add("$Key=$Value") }
    # No byte-order mark: Docker Compose would read it as part of the first setting's name.
    [IO.File]::WriteAllLines($path, [string[]]$lines, (New-Object Text.UTF8Encoding($false)))
}
function Get-AppPort { return (Get-EnvValue 'HL_APP_PORT' '8080') }

# Every password and key is made here, on this machine, once. What is already in .env always wins: Postgres keeps its
# first password in the Docker volume, so a new one would lock the person out of their own data.
function New-Key {
    $bytes = New-Object byte[] 24
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return (-join ($bytes | ForEach-Object { $_.ToString('x2') }))
}
function Get-BackupValue([string]$Key) {
    if (-not (Test-Path -LiteralPath $Script:EnvBackup)) { return '' }
    foreach ($line in [IO.File]::ReadAllLines($Script:EnvBackup)) {
        if ($line -match "^\s*$([regex]::Escape($Key))\s*=\s*(.*?)\s*$") { return $Matches[1] }
    }
    return ''
}
function Get-MissingKeys {
    if (-not (Test-Path -LiteralPath (Get-EnvPath))) { return $Script:KeyNames }
    return @($Script:KeyNames | Where-Object { -not (Get-EnvValue $_) })
}
function Save-EnvBackup {
    # Only a complete file: a damaged .env must never replace the good copy.
    if (@(Get-MissingKeys).Count -gt 0) { return }
    Copy-Item -LiteralPath (Get-EnvPath) -Destination $Script:EnvBackup -Force
}
function Test-DataVolume {
    # $null means Docker did not say, which is not the same as no.
    $volume = "$($Script:Project)_pgdata"
    if (-not (Invoke-Tool 'docker' @('volume', 'ls', '-q', '--filter', "name=$volume"))) { return $null }
    return (@($Script:LastOutput -split "`n" | Where-Object { $_.Trim() -eq $volume }).Count -gt 0)
}
function Stop-IfDataWouldLockOut {
    # No password anywhere. A new one is only safe if there is no data for it to lock.
    $volume = Test-DataVolume
    if ($null -eq $volume) { throw 'Docker did not say whether your data exists, so setup did not make new passwords. Run this file again.' }
    if (-not $volume) { return }
    Write-Log 'STOPPED: .env and its backup are gone and the data volume exists.'
    Your-Turn -NoWait -Lines @(
        'Your settings file (.env) and its backup are both gone, but your simulator data is still here in Docker.',
        'The data is locked with the password that was in that file. A new password would not open it, so setup stopped here.',
        'Nothing has been changed or deleted. You have two choices:',
        "  1  Keep your data: put your old .env back in $($Script:LabDir), from a backup or another copy, then run this file again.",
        '  2  Start fresh: this deletes all your simulator data and tracker progress. In Docker Desktop, delete the',
        "     $($Script:Project) containers under Containers and $($Script:Project)_pgdata under Volumes, then run this file again.",
        'Not sure? Choose 1 first, or ask Kevin. Setup will not delete anything by itself.')
    exit 1
}
function Repair-EnvFile {
    $path = Get-EnvPath
    if (-not (Test-Path -LiteralPath $path)) {
        if (Test-Path -LiteralPath $Script:EnvBackup) {
            Copy-Item -LiteralPath $Script:EnvBackup -Destination $path
            Say 'Your settings file was missing, so setup restored it from its backup. Your passwords still match your data.'
        } else {
            Stop-IfDataWouldLockOut
            Copy-Item -LiteralPath (Join-Path $Script:LabDir '.env.example') -Destination $path
        }
    }
    $made = 0
    foreach ($key in $Script:KeyNames) {
        if (Get-EnvValue $key) { continue }
        $value = Get-BackupValue $key
        if (-not $value) {
            if ($key -eq 'POSTGRES_PASSWORD') { Stop-IfDataWouldLockOut }
            $value = New-Key
            $made++
        }
        Set-EnvValue $key $value
        # The running app has the old value: this sends it through the usual restart, which asks first if a build is running.
        Remove-Item -LiteralPath (Join-Path $Script:StateDir 'app-version.txt') -ErrorAction SilentlyContinue
    }
    if ($made -gt 0) { Say "Made $made new passwords and keys, just for this install. Find them on Connections in the Control Center." }
    Save-EnvBackup
}
function Get-PgPort { return (Get-EnvValue 'HL_PG_PORT' '5432') }
function Get-AppUrl { return "http://localhost:$(Get-AppPort)" }

# ---------------------------------------------------------------------------------------------- docker
function Test-Docker {
    if (-not (Test-Command 'docker')) { return $false }
    $ok = Invoke-Tool 'docker' @('info', '--format', '{{.OSType}}')
    return ($ok -and $Script:LastOutput -match 'linux')
}
function Wait-Docker([int]$Seconds) {
    $waited = 0
    while ($waited -lt $Seconds) {
        if (Test-Docker) { return $true }
        if ($Script:LastOutput -match '^windows') {
            Say 'Docker is in Windows-containers mode. Switching it to Linux containers.'
            $cli = Join-Path $env:ProgramFiles 'Docker\Docker\DockerCli.exe'
            if (Test-Path -LiteralPath $cli) { Invoke-Tool $cli @('-SwitchLinuxEngine') | Out-Null }
        }
        Start-Sleep -Seconds 5
        $waited += 5
        if (($waited % 30) -eq 0) { Say "Still waiting for Docker... ($waited s)" }
    }
    return $false
}
function Find-DockerDesktop {
    foreach ($path in @((Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'), (Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\Docker Desktop.exe'))) {
        if (Test-Path -LiteralPath $path) { return $path }
    }
    return ''
}
function Open-DockerDesktop {
    if ((Test-Command 'docker') -and (Invoke-Tool 'docker' @('desktop', 'start', '--detach'))) { return }
    $exe = Find-DockerDesktop
    if ($exe) { Start-Process -FilePath $exe | Out-Null }
}
function Start-DockerEngine {
    Open-DockerDesktop
    Your-Turn -NoWait -Lines @(
        'Docker Desktop is opening. If it shows an agreement, press Accept.',
        'If it asks you to sign in or fill in a survey, press Skip.',
        'Setup is waiting for it and carries on by itself.'
    )
    if (Wait-Docker 45) { return }

    # Known cause: the CLI is pointed at the wrong engine.
    if ((Invoke-Tool 'docker' @('context', 'ls')) -and $Script:LastOutput -match 'desktop-linux') {
        Say 'Pointing Docker at the Docker Desktop engine.'
        Invoke-Tool 'docker' @('context', 'use', 'desktop-linux') | Out-Null
    }
    if (Wait-Docker 75) { return }

    # Known cause: WSL stopped or is out of date. Docker Desktop has to stop first, or reopening it does nothing.
    Say 'Docker has not started. Updating and restarting WSL, then trying again.'
    Invoke-Tool 'docker' @('desktop', 'stop') | Out-Null
    Update-Wsl | Out-Null
    Stop-WslIfOnlyDocker | Out-Null
    Open-DockerDesktop
    if (Wait-Docker 150) { return }

    # Known cause: this account is not in the docker-users group yet, or the sign-in is older than the install.
    $asked = Join-Path $Script:StateDir 'docker-group.done'
    if (-not (Test-DockerGroup) -and -not (Test-Path -LiteralPath $asked)) {
        Set-Content -LiteralPath $asked -Value (Get-Date -Format 's') -Encoding Ascii
        Invoke-Elevated 'Add-LocalGroupMember -Group docker-users -Member ([Security.Principal.WindowsIdentity]::GetCurrent().Name)' 'to let your account use Docker' | Out-Null
        Request-Restart 'Windows needs you to sign in again before your account can use Docker.'
    }
    Invoke-Tool 'docker' @('version') | Out-Null
    $Script:LastError = 'Docker Desktop did not start within about 5 minutes, even after restarting WSL.'
}
function Get-ProjectContainers([string]$Project, [switch]$All) {
    $a = @('ps', '-q', '--filter', "label=com.docker.compose.project=$Project")
    if ($All) { $a = @('ps', '-aq', '--filter', "label=com.docker.compose.project=$Project") }
    Invoke-Tool 'docker' $a | Out-Null
    return @($Script:LastOutput -split "`n" | Where-Object { $_ -match '^[0-9a-f]{6,}$' })
}
function Move-BusyPorts {
    # Only called when our own containers are not running, so a busy port belongs to something else.
    $changed = $false
    foreach ($pair in @(@('HL_PG_PORT', 5432, 'Postgres'), @('HL_APP_PORT', 8080, 'the app'))) {
        $current = [int](Get-EnvValue $pair[0] ([string]$pair[1]))
        if (Test-PortFree $current) { continue }
        for ($p = $current + 1; $p -lt $current + 50; $p++) {
            if (Test-PortFree $p) {
                Set-EnvValue $pair[0] ([string]$p)
                Say-Warn "Port $current is taken by another program, so $($pair[2]) now uses port $p."
                $changed = $true
                break
            }
        }
    }
    return $changed
}

# ---------------------------------------------------------------------------------------------- app api
$Script:NoAnswerText = 'The app did not answer for 5 minutes. Nothing was wiped and no build was started.'
function Invoke-Api([string]$Method, [string]$Path, $Body = $null, [int]$TimeoutSec = 20) {
    $args2 = @{ Uri = "http://127.0.0.1:$(Get-AppPort)$Path"; Method = $Method; TimeoutSec = $TimeoutSec; ErrorAction = 'Stop'; UseBasicParsing = $true }
    if ($null -ne $Body) { $args2.Body = ($Body | ConvertTo-Json -Compress); $args2.ContentType = 'application/json' }
    $Script:LastCommand = "$Method $Path"
    try { return Invoke-RestMethod @args2 } catch {
        $Script:LastError = "$Method $Path failed: $($_.Exception.Message)"
        $Script:LastOutput = $_.Exception.Message
        return $null
    }
}
function Test-Health {
    $r = Invoke-Api 'GET' '/api/health'
    return ($r -and $r.ok)
}
function Wait-Health([int]$Seconds) {
    $waited = 0
    while ($waited -lt $Seconds) {
        $started = Get-Date
        if (Test-Health) { return $true }
        Start-Sleep -Seconds 3
        # Real time: a try can spend 20 seconds waiting for an answer before this pause.
        $waited += [math]::Max(3, ((Get-Date) - $started).TotalSeconds)
    }
    return $false
}
# A slow answer must never read as "no data yet": a build wipes whatever is there. $null means five minutes of silence.
function Get-SimStatus {
    for ($try = 1; $try -le 5; $try++) {
        $s = Invoke-Api 'GET' '/api/sim/status' $null 45
        if ($s) { return $s }
        if ($try -eq 1) { Say 'The app is busy. Giving it up to 5 minutes to answer.' }
        Start-Sleep -Seconds 15
    }
    return $null
}
function Get-PackageVersion {
    $f = Join-Path $Script:LabDir 'setup\VERSION'
    if (Test-Path -LiteralPath $f) { return (Get-Content -LiteralPath $f -Raw).Trim() }
    return ''
}
function Test-AppCurrent {
    $f = Join-Path $Script:StateDir 'app-version.txt'
    return ((Test-Path -LiteralPath $f) -and ((Get-Content -LiteralPath $f -Raw).Trim() -eq (Get-PackageVersion)))
}
# Asked of the database, not the app, because the app may be what is busy.
function Get-RunningBuild {
    Push-Location -LiteralPath $Script:LabDir
    try {
        $ok = Invoke-Tool 'docker' @('compose', 'exec', '-T', 'postgres', 'psql', '-U', 'postgres', '-d', 'sim_internal', '-Atc', "SELECT label || ', started ' || to_char(started_at, 'YYYY-MM-DD HH24:MI') || ' UTC' FROM sim.run_log WHERE status = 'running' ORDER BY run_id DESC LIMIT 1")
    } finally { Pop-Location }
    if ($ok) { return "$($Script:LastOutput)".Trim() }
    return ''
}
function Get-DataSize {
    if ($env:HL_SETUP_PRESET) { return $env:HL_SETUP_PRESET }
    if ($Script:BuildWasCancelled) { Say 'You cancelled the build. Choose a size to build again, or close this window to stop for now.' }
    Your-Turn -NoWait -Lines @(
        'How much data should the simulator build? It runs in the background while setup carries on.',
        '  1  Small   6 months, 3 warehouses. About 3 GB and 10 minutes. Only for a quick try: several project stories are missing.',
        '  2  Medium  2 years, all 8 warehouses. About 12 GB and 1 hour. Has everything the eight projects need.',
        '  3  Large   3 years, all 8 warehouses. About 30 GB and 3 hours. The same stories with more volume.',
        "Times are rough, for a typical laptop. This drive has $($Script:Machine.FreeGB) GB free.",
        'You can build another size later from the Control Center.')
    if ($Script:Machine.RamGB -lt 15) { Say-Warn 'Under 16 GB of memory: Large may fail on this computer.' }
    $answer = ''
    while ($answer -notmatch '^[123]$') { $answer = Ask-Text 'Type 1, 2 or 3' '2' }
    return @{ '1' = 'small'; '2' = 'medium'; '3' = 'large' }[$answer]
}
function Start-DataBuild {
    if (-not $Script:Preset) { $Script:Preset = Get-DataSize }
    Say "Starting the $($Script:Preset) build. It runs in the background while setup carries on."
    # Never asks to replace data, so the app refuses this if a finished build exists. A slow answer is fine: the check reads what happened.
    $null = Invoke-Api 'POST' '/api/sim/build' @{ preset = $Script:Preset; confirm = $true } 120
}

# ---------------------------------------------------------------------------------------------- github
# An API call, not 'gh auth status': that fails if any old account is stale, even when the active one works.
function Test-GhAuth { return (Invoke-Tool 'gh' @('api', 'user', '--jq', '.login')) }
function Get-GitHubUser {
    if (Invoke-Tool 'gh' @('api', 'user', '--jq', '.login')) { return $Script:LastOutput.Trim() }
    return ''
}
function Test-GitHubSsh {
    $user = Get-GitHubUser
    Invoke-Tool 'ssh' @('-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new', '-o', 'ConnectTimeout=15', 'git@github.com') | Out-Null
    if ($Script:LastOutput -notmatch 'successfully authenticated') { return $false }
    # "Hi someone-else!" means this key belongs to another account, and pushes would be refused.
    return ((-not $user) -or $Script:LastOutput -match ('Hi ' + [regex]::Escape($user) + '!'))
}
function Test-GitHubAccess {
    if (-not (Test-Command 'gh')) { return $false }
    if (-not (Test-GhAuth)) { return $false }
    if ((Invoke-Tool 'gh' @('config', 'get', 'git_protocol', '--host', 'github.com')) -and $Script:LastOutput.Trim() -eq 'https') { return $true }
    return (Test-GitHubSsh)
}
function Add-SshOver443 {
    $config = Join-Path $env:USERPROFILE '.ssh\config'
    $block = "`r`nHost github.com`r`n  Hostname ssh.github.com`r`n  Port 443`r`n  User git`r`n"
    if ((Test-Path -LiteralPath $config) -and ((Get-Content -LiteralPath $config -Raw) -match 'ssh\.github\.com')) { return }
    Add-Content -LiteralPath $config -Value $block -Encoding Ascii
}
function Connect-GitHub {
    if (-not (Test-GhAuth)) {
        $flags = @('auth', 'login', '--hostname', 'github.com', '--git-protocol', 'ssh', '--web', '--skip-ssh-key', '--scopes', 'admin:public_key')
        $help = (& gh auth login --help 2>&1 | Out-String)
        if ($help -notmatch '--skip-ssh-key') {
            Say 'Your GitHub CLI is too old. Updating it.'
            Invoke-Tool 'winget' @('upgrade', '--id', 'GitHub.cli', '-e', '--source', 'winget', '--silent', '--accept-package-agreements', '--accept-source-agreements') -Live | Out-Null
            Update-Path
            $help = (& gh auth login --help 2>&1 | Out-String)
        }
        if ($help -match '--clipboard') { $flags += '--clipboard' }
        Your-Turn -NoWait -Lines @(
            'GitHub sign-in. No account yet? Create one first at https://github.com/signup',
            'A one-time code appears next. Press Enter, sign in on the page that opens, paste the code, press Authorize.'
        )
        if (-not (Invoke-Interactive 'gh' $flags)) { $Script:LastError = "GitHub sign-in did not finish (exit code $($Script:LastCode))."; return }
    }
    # An SSH key with no passphrase is the one-go choice. An existing key is kept as it is.
    $sshDir = Join-Path $env:USERPROFILE '.ssh'
    $key = Join-Path $sshDir 'id_ed25519'
    New-Item -ItemType Directory -Force -Path $sshDir | Out-Null
    if (-not (Test-Path -LiteralPath $key)) {
        $email = 'harborline'
        if (Invoke-Tool 'git' @('config', '--global', 'user.email')) { $email = $Script:LastOutput.Trim() }
        Say 'Creating an SSH key for GitHub.'
        Invoke-Tool 'ssh-keygen' @('-t', 'ed25519', '-C', $email, '-f', $key, '-q', '-N', '""') | Out-Null
    }
    $title = "harborline-$env:COMPUTERNAME"
    if (-not (Invoke-Tool 'gh' @('ssh-key', 'add', "$key.pub", '--title', $title))) {
        if ($Script:LastOutput -match 'admin:public_key|scope') {
            Your-Turn -NoWait -Lines @('GitHub needs one more permission. Press Enter, then approve it on the page that opens.')
            Invoke-Interactive 'gh' @('auth', 'refresh', '--hostname', 'github.com', '--scopes', 'admin:public_key') | Out-Null
            Invoke-Tool 'gh' @('ssh-key', 'add', "$key.pub", '--title', $title) | Out-Null
        }
    }
    if (Test-GitHubSsh) { return }

    if ($Script:LastOutput -match 'IDENTIFICATION HAS CHANGED|Host key verification failed') {
        Say 'GitHub''s saved host key is out of date. Refreshing it.'
        Invoke-Tool 'ssh-keygen' @('-R', 'github.com') | Out-Null
        if (Test-GitHubSsh) { return }
    }
    if ($Script:LastOutput -match 'Bad owner or permissions') {
        Say 'Repairing the permissions on your SSH settings.'
        $config = Join-Path $env:USERPROFILE '.ssh\config'
        if (Test-Path -LiteralPath $config) { Invoke-Tool 'icacls' @($config, '/inheritance:r', '/grant:r', "$($env:USERNAME):F") | Out-Null }
        if (Test-GitHubSsh) { return }
    }
    if ($Script:LastOutput -match 'port 22|timed out|refused|Could not resolve|unreachable') {
        Say 'Your network blocks the usual SSH port. Using GitHub''s port 443 instead.'
        Add-SshOver443
        if (Test-GitHubSsh) { return }
    }
    # Last resort that always works if GitHub is reachable at all: HTTPS through gh's own login.
    Say-Warn 'SSH to GitHub is not working here, so Git will use HTTPS through your GitHub login instead.'
    Invoke-Tool 'gh' @('config', 'set', 'git_protocol', 'https', '--host', 'github.com') | Out-Null
    Invoke-Tool 'gh' @('auth', 'setup-git') | Out-Null
}
function Test-RepoReady {
    $git = Join-Path $Script:RepoDir '.git'
    if (-not (Test-Path -LiteralPath $git)) { return $false }
    if (-not (Invoke-Tool 'git' @('-C', $Script:RepoDir, 'remote', 'get-url', 'origin'))) { return $false }
    if ($Script:LastOutput -notmatch [regex]::Escape($Script:RepoName)) { return $false }
    if (-not (Invoke-Tool 'git' @('-C', $Script:RepoDir, 'rev-parse', '--verify', 'origin/main'))) { return $false }
    return $true
}
function Test-RepoHooks { return (Test-Path -LiteralPath (Join-Path $Script:RepoDir '.git\hooks\pre-commit')) }
function Copy-RepoTemplate {
    $template = Join-Path $Script:LabDir 'setup\repo-template'
    Get-ChildItem -LiteralPath $template -Recurse -Force -File | ForEach-Object {
        $relative = $_.FullName.Substring($template.Length).TrimStart('\', '/')
        $target = Join-Path $Script:RepoDir $relative
        if (-not (Test-Path -LiteralPath $target)) {
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
            Copy-Item -LiteralPath $_.FullName -Destination $target
        }
    }
}
function New-AnalyticsRepo {
    $user = Get-GitHubUser
    if (-not $user) { $Script:LastError = 'Could not read your GitHub username.'; return }
    $full = "$user/$($Script:RepoName)"
    $onGitHub = Invoke-Tool 'gh' @('repo', 'view', $full, '--json', 'name')
    $git = Join-Path $Script:RepoDir '.git'
    $useHttps = (Invoke-Tool 'gh' @('config', 'get', 'git_protocol', '--host', 'github.com')) -and $Script:LastOutput.Trim() -eq 'https'
    New-Item -ItemType Directory -Force -Path $Script:RepoDir | Out-Null
    # This folder may have been created by an administrator window, which Git refuses until told it is fine.
    Invoke-Tool 'git' @('config', '--global', '--add', 'safe.directory', ($Script:RepoDir -replace '\\', '/')) | Out-Null
    $empty = -not (Get-ChildItem -LiteralPath $Script:RepoDir -Force | Select-Object -First 1)
    if ($onGitHub -and $empty) {
        Say "$full is already on GitHub. Cloning it."
        if (-not (Invoke-Tool 'gh' @('repo', 'clone', $full, $Script:RepoDir))) { return }
    }
    if (-not (Test-Path -LiteralPath $git)) {
        if (-not (Invoke-Tool 'git' @('-C', $Script:RepoDir, 'init', '-b', 'main'))) {
            if (-not (Invoke-Tool 'git' @('-C', $Script:RepoDir, 'init'))) { return }
            Invoke-Tool 'git' @('-C', $Script:RepoDir, 'symbolic-ref', 'HEAD', 'refs/heads/main') | Out-Null
        }
    }
    if ($onGitHub -and -not (Invoke-Tool 'git' @('-C', $Script:RepoDir, 'remote', 'get-url', 'origin'))) {
        $url = "https://github.com/$full.git"
        if (-not $useHttps) { Invoke-Tool 'gh' @('repo', 'view', $full, '--json', 'sshUrl', '--jq', '.sshUrl') | Out-Null; $url = $Script:LastOutput.Trim() }
        if (-not (Invoke-Tool 'git' @('-C', $Script:RepoDir, 'remote', 'add', 'origin', $url))) { return }
    }
    if (Invoke-Tool 'git' @('-C', $Script:RepoDir, 'remote', 'get-url', 'origin')) {
        if ($useHttps -and $Script:LastOutput -match '^git@') { Invoke-Tool 'git' @('-C', $Script:RepoDir, 'remote', 'set-url', 'origin', "https://github.com/$full.git") | Out-Null }
        Invoke-Tool 'git' @('-C', $Script:RepoDir, 'fetch', 'origin') | Out-Null
    }
    $hasRemoteMain = Invoke-Tool 'git' @('-C', $Script:RepoDir, 'rev-parse', '--verify', 'origin/main')
    $hasCommit = Invoke-Tool 'git' @('-C', $Script:RepoDir, 'rev-parse', '--verify', 'HEAD')
    if ($hasRemoteMain -and -not $hasCommit) {
        # Files already in the folder stay as they are; this only adopts GitHub's history.
        Invoke-Tool 'git' @('-C', $Script:RepoDir, 'reset', 'origin/main') | Out-Null
        return
    }
    if ($hasRemoteMain) {
        if (-not (Invoke-Tool 'git' @('-C', $Script:RepoDir, 'merge-base', 'HEAD', 'origin/main'))) {
            $Script:LastOutput = "unrelated histories: this folder and $full on GitHub have different commits."
            $Script:LastError = 'Your folder and the GitHub repository disagree, and setup will not overwrite either.'
        }
        return
    }
    Copy-RepoTemplate
    if (-not $hasCommit) {
        Invoke-Tool 'git' @('-C', $Script:RepoDir, 'add', '-A') | Out-Null
        if (-not (Invoke-Tool 'git' @('-C', $Script:RepoDir, '-c', 'commit.gpgsign=false', 'commit', '-m', 'chore: repository skeleton'))) { return }
    }
    if (-not (Invoke-Tool 'git' @('-C', $Script:RepoDir, 'remote', 'get-url', 'origin'))) {
        Say "Creating $full on GitHub (public, so branch protection works on a free account)."
        $description = 'Data platform and dashboards for Harborline Logistics (practice program)'
        if (-not (Invoke-Tool 'gh' @('repo', 'create', $Script:RepoName, '--public', '--source', $Script:RepoDir, '--remote', 'origin', '--description', $description))) { return }
    }
    # A leftover from an earlier attempt may be on 'master'.
    Invoke-Tool 'git' @('-C', $Script:RepoDir, 'branch', '-M', 'main') | Out-Null
    # Git for Windows brings its own ssh, which can't see the Windows agent the test above used.
    $windowsSsh = Join-Path $env:SystemRoot 'System32\OpenSSH\ssh.exe'
    if (-not $useHttps -and (Test-Path -LiteralPath $windowsSsh)) {
        Invoke-Tool 'git' @('-C', $Script:RepoDir, 'config', 'core.sshCommand', ($windowsSsh -replace '\\', '/')) | Out-Null
    }
    if (-not (Invoke-Tool 'git' @('-C', $Script:RepoDir, 'push', '-u', 'origin', 'main'))) {
        if ($Script:LastOutput -match 'GH007|email privacy') {
            Say 'GitHub keeps your email private. Using your GitHub no-reply address for this repository.'
            Invoke-Tool 'gh' @('api', 'user', '--jq', '.id') | Out-Null
            $id = $Script:LastOutput.Trim()
            Invoke-Tool 'git' @('-C', $Script:RepoDir, 'config', 'user.email', "$id+$user@users.noreply.github.com") | Out-Null
            Invoke-Tool 'git' @('-C', $Script:RepoDir, '-c', 'commit.gpgsign=false', 'commit', '--amend', '--reset-author', '--no-edit') | Out-Null
            Invoke-Tool 'git' @('-C', $Script:RepoDir, 'push', '-u', 'origin', 'main') | Out-Null
        }
    }
}
function Install-RepoHooks {
    Push-Location -LiteralPath $Script:RepoDir
    try {
        Say 'Installing the commit checks (a few minutes the first time).'
        if (Invoke-Tool 'pre-commit' @('install', '--install-hooks') -Live) { return }
        if ($Script:LastOutput -match 'Cowardly refusing') {
            # A setting in this repository alone is safe to clear; a global one may be company policy.
            $Script:LastError = $Script:LastOutput
            if (Invoke-Tool 'git' @('config', '--local', '--unset-all', 'core.hooksPath')) { Invoke-Tool 'pre-commit' @('install', '--install-hooks') -Live | Out-Null }
        }
    } finally { Pop-Location }
}
function Test-RepoProtected {
    $user = Get-GitHubUser
    if (-not $user) { return $false }
    if (-not (Invoke-Tool 'gh' @('api', "repos/$user/$($Script:RepoName)/rulesets", '--jq', '.[].name'))) { return $false }
    return ($Script:LastOutput -match 'protect main')
}

# ---------------------------------------------------------------------------------------------- dbeaver
function Find-DBeaver {
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
        if (-not $base) { continue }
        $exe = Join-Path $base 'DBeaver\dbeaver.exe'
        if (Test-Path -LiteralPath $exe) { return $exe }
    }
    return ''
}
function Get-DBeaverSources { return (Join-Path $env:APPDATA 'DBeaverData\workspace6\General\.dbeaver\data-sources.json') }
function Test-DBeaverConnections {
    if (-not (Find-DBeaver)) { return $false }
    $file = Get-DBeaverSources
    return ((Test-Path -LiteralPath $file) -and ((Get-Content -LiteralPath $file -Raw) -match 'Harborline WMS'))
}
function Add-DBeaverConnections {
    # One connection per system, so an editor can never be on the wrong database or on the sample database.
    # One launch: a second copy starting before the first has finished would find the workspace locked.
    $exe = Find-DBeaver
    if (-not $exe) { $Script:LastError = 'DBeaver was not found.'; return }
    $pw = Get-EnvValue 'HL_ETL_PASSWORD'
    $port = Get-PgPort
    $launch = @('-nosplash')
    foreach ($c in @(@('wms', 'WMS'), @('ax2012', 'AX 2012'), @('iot', 'IoT'), @('tracker', 'Tracker'))) {
        $spec = "driver=postgresql|host=localhost|port=$port|database=$($c[0])|user=etl_reader|password=$pw|savePassword=true|name=Harborline $($c[1])|create=true|save=true|connect=false|openConsole=false"
        $launch += @('-con', ('"' + $spec + '"'))
    }
    Your-Turn -NoWait -Lines @('DBeaver is opening. If it offers to create a sample database, press No.')
    Start-Process -FilePath $exe -ArgumentList $launch | Out-Null
    for ($waited = 0; $waited -lt 60; $waited += 5) {
        if (Test-DBeaverConnections) { return }
        Start-Sleep -Seconds 5
    }
    $Script:LastOutput = 'DBeaver did not save the connections within a minute. If it is showing a dialog, answer it, then run this file again.'
}

# ---------------------------------------------------------------------------------------------- team chats
# No AI site lets a script create chats: he signs in and sends each first message, the setup file does the rest.
function Get-TeamList {
    $path = Join-Path $Script:LabDir 'team\prompts\index.tsv'
    foreach ($line in [IO.File]::ReadAllLines($path)) {
        $f = $line -split "`t"
        if ($f.Count -ge 5) { [pscustomobject]@{ Id = $f[0]; Name = $f[1]; Title = $f[2]; Zip = ($f[3] -eq 'yes'); Item = $f[4] } }
    }
}
function Get-MissingChats {
    $saved = Invoke-Api 'GET' '/api/handbook/team-links'
    if (-not $saved) { return (Get-TeamList) }
    return @(Get-TeamList | Where-Object { -not $saved.links.($_.Id) })
}
function Test-TeamChats {
    if (-not (Test-Health)) { return $false }
    return (@(Get-MissingChats).Count -eq 0)
}
function Get-ChatLinkProblem([string]$Url) {
    if ($Url -notmatch '^https://[^\s"]+$') { return 'That is not a web address. It should start with https://' }
    if ($Url -match '[?&](temporary-chat|temporary=|incognito)') { return 'That is a temporary chat, which cannot be reopened. Start a normal chat instead.' }
    if ($Url -match '^https://[^/?#]+(/(new|app|chat|chats|c)?/?)?([?#].*)?$') {
        return 'That is the site''s start page, not a chat. Send the first message, wait for the reply, then copy the address again.'
    }
    return ''
}
function Get-AiSite {
    # Any AI website works. Asked once, then remembered here.
    $file = Join-Path $Script:StateDir 'ai-site.txt'
    if (Test-Path -LiteralPath $file) {
        $parts = (Get-Content -LiteralPath $file -Raw).Trim() -split '\|', 2
        if ($parts.Count -eq 2) { return [pscustomobject]@{ Name = $parts[0]; Url = $parts[1] } }
    }
    Your-Turn -NoWait -Lines @(
        'Your colleagues can live on any AI website. Pick the one you use:',
        '  1  ChatGPT    2  Claude    3  Gemini    4  Microsoft Copilot    5  Another site'
    )
    $choice = ''
    while ($choice -notmatch '^[1-5]$') { $choice = Ask-Text 'Your choice' '1' }
    switch ($choice) {
        '1' { $site = [pscustomobject]@{ Name = 'ChatGPT'; Url = 'https://chatgpt.com/' } }
        '2' { $site = [pscustomobject]@{ Name = 'Claude'; Url = 'https://claude.ai/new' } }
        '3' { $site = [pscustomobject]@{ Name = 'Gemini'; Url = 'https://gemini.google.com/app' } }
        '4' { $site = [pscustomobject]@{ Name = 'Microsoft Copilot'; Url = 'https://copilot.microsoft.com/' } }
        '5' {
            $url = ''
            while ($url -notmatch '^https://[^\s"|]+$') { $url = Ask-Text 'The address where a new chat starts (https://...)' }
            $site = [pscustomobject]@{ Name = ($url -replace '^https://(www\.)?([^/]+).*$', '$2'); Url = $url }
        }
    }
    [IO.File]::WriteAllText($file, "$($site.Name)|$($site.Url)", [Text.Encoding]::ASCII)
    $null = Invoke-Api 'PUT' '/api/handbook/team-site' @{ name = $site.Name; url = $site.Url }
    return $site
}
function Open-Url([string]$Url) { try { Start-Process $Url | Out-Null } catch { Say "Open this in your browser: $Url" } }
function Start-TeamChats {
    $zip = Join-Path $Script:Root 'harborline-chat.zip'
    $source = Join-Path $Script:LabDir 'setup\harborline-chat.zip'
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination $zip -Force
        try { Start-Process explorer.exe -ArgumentList ('/select,"' + $zip + '"') | Out-Null } catch { }
    }
    $site = Get-AiSite
    $memoryDone = Join-Path $Script:StateDir 'ai-memory.done'
    if (-not (Test-Path -LiteralPath $memoryDone)) {
        if ($site.Name -eq 'ChatGPT') {
            Open-Url 'https://chatgpt.com/#settings/Personalization'
            $how = 'In Settings > Personalization, turn off saved memories and chat history, so colleagues cannot read each other''s chats.'
        } else {
            Open-Url $site.Url
            $how = 'If this site has a memory that works across chats, turn it off in its settings, so colleagues cannot read each other''s chats.'
        }
        Your-Turn -Lines @("$($site.Name) is opening. Sign in if it asks.", $how)
        Set-Content -LiteralPath $memoryDone -Value (Get-Date -Format 's') -Encoding Ascii
    }
    $missing = @(Get-MissingChats)
    Your-Turn -NoWait -Lines @(
        "Now one chat per colleague: $($missing.Count) to go, about a minute each. You can stop at any point and carry on later.",
        "IMPORTANT: send harborline-chat.zip WITH the prompt, in the same first message, every time.",
        "The zip holds the handbook, tickets and brand kit. Without it your colleague can't see the company and will guess.",
        "A folder window shows the zip: keep it open, you drag it into each chat.",
        "Only if $($site.Name) refuses the zip: send the prompt alone, then attach single files when a question needs them."
    )
    $n = 0
    foreach ($person in $missing) {
        $n++
        $text = [IO.File]::ReadAllText((Join-Path $Script:LabDir "team\prompts\$($person.Id).txt"))
        $hello = if ($person.Zip) { 'Hi, I just joined and attached the Harborline zip. Say hello in one line and tell me if you can open it; I will come back with questions.' }
        else { 'Hi, I just joined. Say hello in one line; I will come back with questions.' }
        Set-Clipboard -Value ($text + "`r`n`r`n" + $hello)
        Open-Url $site.Url
        Write-Host ''
        Write-Host ("    [{0}/{1}] {2}, {3}" -f $n, $missing.Count, $person.Name, $person.Title) -ForegroundColor Yellow
        if ($person.Zip) {
            Write-Host '    In the new tab, all in ONE message:' -ForegroundColor Yellow
            Write-Host '      1. Drag in harborline-chat.zip and wait until it finishes uploading.' -ForegroundColor Yellow
            Write-Host '      2. Press Ctrl+V to paste the prompt.' -ForegroundColor Yellow
            Write-Host '      3. Press Enter.' -ForegroundColor Yellow
        }
        else { Write-Host '    In the new tab: press Ctrl+V, press Enter. This one does not need the zip.' -ForegroundColor Yellow }
        Write-Host '    When it replies, copy the address from the top of the browser and paste it here.' -ForegroundColor Yellow
        while ($true) {
            $answer = Ask-Text 'Chat address (Enter skips this person, later stops for now)'
            if (-not $answer) { Say 'Skipped.'; break }
            if ($answer -match '^(later|stop|q|quit)$') { Say 'Stopping here. Run this file again to carry on.'; return }
            $problem = Get-ChatLinkProblem $answer
            if ($problem) { Say-Warn $problem; continue }
            $saved = Invoke-Api 'PUT' "/api/handbook/team-links/$($person.Id)" @{ url = $answer; item = $person.Item }
            if ($saved) { Say-Good 'Saved.' } else { Say-Warn "Could not save that: $($Script:LastError)" }
            break
        }
    }
}

# ---------------------------------------------------------------------------------------------- errors
# Pattern seen in the output -> what to try. Each fix runs at most once per step, so nothing can loop.
function Get-KnownFixes {
    return @(
        @{ Name = 'winget sources'; Match = 'Failed in attempting to update the source|Failed when opening source|Failed when searching source|No package found matching|Installer hash does not match|0x8a15'; Fix = {
                Say 'Refreshing winget''s package list.'
                if (-not (Invoke-Tool 'winget' @('source', 'update', '--name', 'winget'))) {
                    Invoke-Elevated 'winget source reset --force' 'to repair winget' | Out-Null
                }
            }
        },
        @{ Name = 'winget downloads'; Match = '0x801901a0|0x80d03002'; Fix = {
                Say 'Switching winget away from Delivery Optimization.'
                $settings = Join-Path $env:LOCALAPPDATA 'Packages\Microsoft.DesktopAppInstaller_8wekyb3d8bbwe\LocalState\settings.json'
                if (-not (Test-Path -LiteralPath $settings)) { Set-Content -LiteralPath $settings -Value '{"network":{"downloader":"wininet"}}' -Encoding Ascii }
            }
        },
        @{ Name = 'terminal framework'; Match = '0x80073CF3|depends on a framework'; Fix = {
                Invoke-Tool 'winget' @('install', '--id', 'Microsoft.UI.Xaml.2.8', '-e', '--source', 'winget', '--accept-package-agreements', '--accept-source-agreements') | Out-Null
            }
        },
        @{ Name = 'docker engine'; Match = 'docker_engine|dockerDesktopLinuxEngine|Cannot connect to the Docker daemon|failed to connect to the docker API|error during connect'; Fix = { Start-DockerEngine } },
        @{ Name = 'busy ports'; Match = 'port is already allocated|address already in use|Only one usage of each socket|ports are not available|forbidden by its access permissions'; Fix = {
                Invoke-Tool 'docker' @('compose', '-p', $Script:Project, 'down') | Out-Null
                Move-BusyPorts | Out-Null
            }
        },
        @{ Name = 'WSL'; Match = 'WSL.*(terminated|unexpected|kernel)|WSL 2 requires|kernel file is not found|0x800701bc|0x1bc|wsl update failed|REGDB_E_CLASSNOTREG|Wsl/InstallDistro|exit code: 1603|Wsl/CallMsi|0x8000ffff|Catastrophic failure|E_UNEXPECTED|0x80004005'; Fix = {
                Invoke-Tool 'docker' @('desktop', 'stop') | Out-Null
                Update-Wsl | Out-Null
                Stop-WslIfOnlyDocker | Out-Null
                Open-DockerDesktop
                Wait-Docker 150 | Out-Null
            }
        },
        @{ Name = 'WSL service'; Match = '0x80070422'; Fix = { Invoke-Elevated 'Set-Service WSLService -StartupType Automatic; Start-Service WSLService' 'to turn the WSL service back on' | Out-Null } },
        @{ Name = 'virtual machine service'; Match = '0x80370114'; Fix = { Invoke-Elevated 'Start-Service vmcompute' 'to start the virtual machine service' | Out-Null } },
        @{ Name = 'WSL feature'; Match = '0x8007019e'; Fix = { Invoke-Elevated 'wsl --install --no-distribution' 'to turn WSL on' | Out-Null } },
        @{ Name = 'hypervisor'; Match = '0x80370102|HCS_E_HYPERV_NOT_INSTALLED'; Fix = {
                Invoke-Elevated 'bcdedit /set hypervisorlaunchtype Auto' 'to turn the Windows hypervisor on' | Out-Null
                Request-WslRestart
            }
        },
        @{ Name = 'Docker credentials'; Match = 'error getting credentials|docker-credential-desktop'; Fix = {
                $config = Join-Path $env:USERPROFILE '.docker\config.json'
                if (Test-Path -LiteralPath $config) {
                    Say 'Docker''s saved-login helper is missing. Turning it off (a backup is kept next to the file).'
                    Copy-Item -LiteralPath $config -Destination "$config.bak" -Force
                    [IO.File]::WriteAllText($config, ([IO.File]::ReadAllText($config) -replace '"credsStore"', '"credStore"'), (New-Object Text.UTF8Encoding($false)))
                }
            }
        },
        @{ Name = 'disk space'; Match = 'no space left on device'; Fix = {
                Say 'Docker is out of disk space. Clearing its build cache, which only costs time.'
                Invoke-Tool 'docker' @('builder', 'prune', '-f') | Out-Null
            }
        },
        @{ Name = 'slow database'; Match = 'dependency failed to start|is unhealthy'; Fix = {
                Say 'Postgres is taking longer than expected. Waiting a minute and trying again.'
                Start-Sleep -Seconds 60
            }
        },
        @{ Name = 'GitHub login'; Match = 'HTTP 401|Bad credentials|sign-in did not finish|failed to authenticate|slow_down|expired_token|token_expired'; Fix = {
                Invoke-Tool 'gh' @('auth', 'logout', '--hostname', 'github.com') | Out-Null
                Connect-GitHub
            }
        },
        @{ Name = 'Git ownership'; Match = 'dubious ownership'; Fix = {
                Invoke-Tool 'git' @('config', '--global', '--add', 'safe.directory', ($Script:RepoDir -replace '\\', '/')) | Out-Null
            }
        },
        @{ Name = 'uv certificates'; Match = 'invalid peer certificate|UnknownIssuer'; Fix = {
                Say 'Your network inspects secure traffic. Telling uv to use the system''s certificates.'
                $env:UV_SYSTEM_CERTS = 'true'
            }
        },
        @{ Name = 'uv overwrite'; Match = 'Executable already exists'; Fix = { Invoke-Tool 'uv' @('tool', 'install', 'pre-commit', '--force') | Out-Null } },
        @{ Name = 'Go for commit checks'; Match = 'CERTIFICATE_VERIFY_FAILED|go\.dev/dl'; Fix = {
                Install-Package -Ids @('GoLang.Go') -Label 'Go (one of the commit checks needs it)' | Out-Null
            }
        },
        @{ Name = 'old VS Code'; Match = 'not compatible with the current version'; Fix = {
                Invoke-Tool 'winget' @('upgrade', '--id', 'Microsoft.VisualStudioCode', '-e', '--source', 'winget', '--silent', '--accept-package-agreements', '--accept-source-agreements') -Live | Out-Null
            }
        },
        @{ Name = 'landing folder'; Match = 'cannot write to the landing folder|Permission denied: .*/data/landing'; Fix = {
                Say 'The app cannot write to the landing folder. Restarting it, which repairs the folder.'
                Push-Location -LiteralPath $Script:LabDir
                try { Invoke-Tool 'docker' @('compose', 'restart', 'app') | Out-Null } finally { Pop-Location }
                Wait-Health 120 | Out-Null
            }
        },
        @{ Name = 'network'; Match = 'TLS handshake|i/o timeout|timed out|Temporary failure|connection reset|forcibly closed|unexpected EOF|Service Unavailable|Bad Gateway|no such host|dial tcp|ECONNRESET|ETIMEDOUT|EAI_AGAIN|Could not resolve'; Fix = {
                Say 'That looks like a network hiccup. Waiting 20 seconds and trying again.'
                Start-Sleep -Seconds 20
            }
        },
        @{ Name = 'network, second try'; Match = 'TLS handshake|i/o timeout|timed out|Temporary failure|connection reset|forcibly closed|unexpected EOF|Service Unavailable|Bad Gateway|no such host|dial tcp|ECONNRESET|ETIMEDOUT|EAI_AGAIN|Could not resolve'; Fix = {
                Say 'Still a network problem. Waiting a minute and trying once more.'
                Start-Sleep -Seconds 60
            }
        }
    )
}
# Problems only a person can solve: pattern -> what to do. Shown in the stop report. handbook\known-issues.html has them all.
function Get-KnownHint([string]$Text) {
    $hints = @(
        @('cannot write to the landing folder|Permission denied: .*/data/landing', 'The app still cannot write to the landing folder after a restart. Check your antivirus, then run this file again. Kevin can read this report.'),
        @('did not answer|stopped answering', 'The app is busy or stuck. Your data is safe. Wait a few minutes and run this file again.'),
        @('is not recognized|not recognized as|CommandNotFoundException', 'A terminal only sees programs installed before it opened. Close this window, open a new one, and run this file again.'),
        @('disabled by Group Policy|Organization policies are preventing|blocked by group policy|0x800704ec', 'A company policy blocks this. Ask whoever manages this computer, or use another computer.'),
        @('You cancelled the installation|Administrator permission was not given|800704c7', 'The Windows permission prompt was refused. Run this file again and press Yes.'),
        @('0x80370102|HCS_E_HYPERV_NOT_INSTALLED', 'Virtualization is not reaching Windows. Turn on VT-x or SVM in the BIOS. Inside a virtual machine, turn on nested virtualization. Update old VMware or VirtualBox.'),
        @('0x80370114', 'Windows Security > App & browser control > Exploit protection > Program settings > vmcompute.exe > Control Flow Guard: untick Override, then restart.'),
        @('Usage: wsl', 'This Windows is missing a WSL update. Run Windows Update, restart, and run this file again.'),
        @('0x80070003', 'Settings > System > Storage > Advanced storage settings > Where new content is saved: set new apps to C:.'),
        @('ScriptContainedMaliciousContent|blocked by your antivirus', 'Antivirus blocked this. Review it under Windows Security > Protection history.'),
        @('0x80072efd', 'winget cannot reach the internet, usually a proxy. Run: winget settings --enable ProxyCommandLineOptions (as administrator), or try another network.'),
        @('Installer hash does not match|0x8a150011', 'The vendor released a new installer before winget caught up. Try again tomorrow, or install this one from the vendor''s site, then run this file again.'),
        @('toomanyrequests|429 Too Many|pull rate limit', 'Docker Hub limits downloads to 100 per 6 hours per network. Wait, change network, or run: docker login'),
        @('x509: certificate|SSL certificate problem|CERTIFICATE_VERIFY_FAILED|invalid peer certificate|UnknownIssuer', 'Your network inspects secure traffic. Ask IT for its root certificate, or try another network.'),
        @('failed to fetch oauth token|failed to authorize', 'A stale Docker Hub login. Run: docker logout, then run this file again.'),
        @('Sign in enforced|sign-in enforced', 'Your organisation requires a Docker sign-in. Sign in to Docker Desktop with your work account, or ask IT.'),
        @('no space left on device', 'The disk is full. Free some space, or raise Docker Desktop > Settings > Resources > Disk usage limit.'),
        @('exit code: 137|OOMKilled|terminated by signal 9', 'It ran out of memory. Close other apps, raise memory= in your .wslconfig, or build a smaller data set in the Control Center.'),
        @('dependency failed to start|is unhealthy', 'Postgres did not become healthy. Its log is in this report: Kevin can read it.'),
        @("env_file|'compose' is not a docker command", 'Docker Desktop is too old. Update it from its own menu, then run this file again.'),
        @('certificate has expired or is not yet valid', 'Docker''s clock drifted after the computer slept. Restart Docker Desktop.'),
        @('error from sender|Access is denied', 'Something is blocking a file, usually antivirus. The path is in this report. Add an exclusion or grant read access.'),
        @('verify your email|email.*verif', 'Verify your email at github.com/settings/emails, then run this file again.'),
        @('Upgrade to GitHub Pro', 'Branch protection needs a public repository on a free account. Make it public, or skip this: nothing else depends on it.'),
        @('fetch first|non-fast-forward|Updates were rejected|unrelated histories', 'Your folder and the GitHub repository have different commits. Ask Kevin. Do not force-push.'),
        @('Cowardly refusing', 'A global Git setting (core.hooksPath) blocks the commit checks. See it with: git config --show-origin --get-all core.hooksPath. It may be company policy, so ask before removing it.'),
        @('Push cannot contain secrets', 'GitHub found a secret in the commit. Remove that file, then: git commit --amend --all'),
        @('Changes must be made through a pull request', 'main is protected. Push a branch and open a pull request instead.'),
        @('invalid format', 'Your SSH key file is damaged. Do not overwrite it blindly: ask Kevin.'),
        @('XHR failed|Failed Installing Extensions|Error while installing extensions', 'VS Code cannot reach its Marketplace. Allow marketplace.visualstudio.com and *.vsassets.io, or install the extensions from inside VS Code.'),
        @('os error 5|Failed to update Windows PE', 'Antivirus blocked uv. Add an exclusion for it, then run this file again.'),
        @('filename or extension is too long', 'Windows long paths are off. Set LongPathsEnabled to 1 under HKLM\SYSTEM\CurrentControlSet\Control\FileSystem, as administrator.'),
        @('python-build-standalone', 'uv cannot download Python, usually a proxy. Set HTTPS_PROXY for your user, or try another network.'),
        @("Can't lock workspace", 'DBeaver is already starting. In its dialog press Abort, never Ignore, then run this file again.')
    )
    foreach ($h in $hints) { if ($Text -match $h[0]) { return $h[1] } }
    return ''
}
function Stop-WithError([string]$Title) {
    $info = @(
        "Harborline Lab setup stopped at: $Title",
        "Time: $(Get-Date -Format 's')",
        "Command: $($Script:LastCommand)",
        "Exit code: $($Script:LastCode)"
    )
    if ($Script:LastError) { $info += "Message: $($Script:LastError)" }
    $hint = Get-KnownHint "$($Script:LastOutput)`n$($Script:LastError)"
    if ($hint) { $info += "What this usually means: $hint" }
    $tail = @($Script:LastOutput -split "`n" | Select-Object -Last 40)
    $report = ($info + '' + 'Output:' + $tail + '' + 'Machine: Windows setup file. Setup skips finished steps when it runs again.') -join "`r`n"
    [IO.File]::WriteAllText($Script:ErrorFile, $report, (New-Object Text.UTF8Encoding($false)))
    try { Set-Clipboard -Value $report } catch { }
    Write-Host ''
    Write-Host '    ============================== SETUP STOPPED ==============================' -ForegroundColor Red
    foreach ($line in ($report -split "`r`n")) { Write-Host "    $line" -ForegroundColor Red }
    Write-Host '    ===========================================================================' -ForegroundColor Red
    Write-Host ''
    Write-Host '    What to do (this report is already on your clipboard):' -ForegroundColor Yellow
    Write-Host '    1. Paste it into your chat with Kevin O''Hara (IT help), or any AI chat. His prompt is in handbook\team.html.' -ForegroundColor Yellow
    Write-Host '    2. Do what he says.' -ForegroundColor Yellow
    Write-Host '    3. Run this file again. Everything finished so far is skipped.' -ForegroundColor Yellow
    Write-Host "    The report is also saved at $($Script:ErrorFile)" -ForegroundColor Yellow
    Write-Host '    Every known error is listed in handbook\known-issues.html, with what to do.' -ForegroundColor Yellow
    exit 1
}

# ---------------------------------------------------------------------------------------------- the step runner
function Step {
    param([string]$Title, [scriptblock]$Check, [scriptblock]$Fix, [switch]$Optional, [switch]$NoFreshWindow)
    $Script:StepNo++
    Write-Host ''
    Write-Host ("[{0}/{1}] {2}" -f $Script:StepNo, $Script:StepTotal, $Title) -ForegroundColor Cyan
    Write-Log "STEP $Title"
    $passed = [bool](& $Check | Select-Object -Last 1)
    if ($passed) {
        Say-Good $(if ($Script:PassText) { $Script:PassText } else { 'Already done.' })
        [void]$Script:Results.Add([pscustomobject]@{ Step = $Title; Status = 'skipped' })
        return
    }
    $used = @{}
    for ($attempt = 1; $attempt -le 4; $attempt++) {
        $Script:LastError = ''
        try { & $Fix | Out-Null } catch { $Script:LastError = $_.Exception.Message }
        Update-Path
        # Saved now: the check below runs commands of its own, and the report must show the one that failed.
        $failure = "$($Script:LastOutput)`n$($Script:LastError)".Trim()
        $failedCommand = $Script:LastCommand
        $failedCode = $Script:LastCode
        $failedMessage = $Script:LastError
        if ([bool](& $Check | Select-Object -Last 1)) {
            Say-Good 'Done.'
            [void]$Script:Results.Add([pscustomobject]@{ Step = $Title; Status = 'done' })
            return
        }
        # Some checks find out the reason themselves, such as a data build that failed.
        if ($Script:LastError -and $failure -notmatch [regex]::Escape($Script:LastError)) {
            $failure = "$failure`n$($Script:LastOutput)`n$($Script:LastError)".Trim()
            $failedMessage = $Script:LastError
        }
        # A slow answer from the app on this machine is not a network problem, and a retry would only repeat the call.
        $ownApp = $failedCommand -match '^(GET|POST|PUT) /api/'
        $next = @(Get-KnownFixes | Where-Object { -not ($ownApp -and $_.Name -match '^network') -and -not $used[$_.Name] -and $failure -match $_.Match } | Select-Object -First 1)
        if ($next.Count -eq 0) { break }
        $used[$next[0].Name] = $true
        Say "Known problem ($($next[0].Name)). Fixing it and trying again."
        Write-Log "HEAL $($next[0].Name)"
        try { & $next[0].Fix | Out-Null } catch { $Script:LastError = $_.Exception.Message }
    }
    $Script:LastOutput = $failure
    $Script:LastCommand = $failedCommand
    $Script:LastCode = $failedCode
    $Script:LastError = $failedMessage
    if (-not $NoFreshWindow) { Restart-InFreshWindow }
    if ($Optional) {
        Say-Warn "Skipped for now: $Title. Nothing else depends on it. Run this file again to retry."
        $hint = Get-KnownHint $failure
        if ($hint) { Say-Warn $hint }
        Write-Log "OPTIONAL FAILED $Title :: $($Script:LastCommand) :: $($Script:LastOutput)"
        [void]$Script:Results.Add([pscustomobject]@{ Step = $Title; Status = 'needs a retry' })
        return
    }
    Stop-WithError $Title
}

# ---------------------------------------------------------------------------------------------- updates
# A new version is the same as running a newer zip: get the files, then run the new setup file from them. Finished steps
# skip, and the Simulator and Start steps do the rest. Nothing here may stop or slow down someone with no internet.
function Get-VersionIn([string]$Dir) {
    try {
        $file = Join-Path $Dir 'setup\VERSION'
        if (Test-Path -LiteralPath $file) { return ([IO.File]::ReadAllText($file)).Trim() }
    } catch { }
    return ''
}
function Test-VersionText([string]$Text) { return ($Text -match '^[0-9]{1,9}(\.[0-9]{1,9}){0,5}$') }
function Test-NewerVersion([string]$A, [string]$B) {
    # Part by part as numbers: as text, 2026.11.10 would sort before 2026.11.9. Anything unreadable is never newer.
    if (-not (Test-VersionText $A) -or -not (Test-VersionText $B)) { return $false }
    $x = @($A -split '\.')
    $y = @($B -split '\.')
    for ($i = 0; $i -lt [math]::Max($x.Count, $y.Count); $i++) {
        $p = 0L
        $q = 0L
        if ($i -lt $x.Count) { $p = [long]$x[$i] }
        if ($i -lt $y.Count) { $q = [long]$y[$i] }
        if ($p -gt $q) { return $true }
        if ($p -lt $q) { return $false }
    }
    return $false
}
function Get-UpdateRepo {
    # The environment wins, for testing and forks. Windows can't hold an empty variable, so a single space also means off.
    $text = ''
    if (Test-Path Env:\HL_UPDATE_REPO) { $text = $env:HL_UPDATE_REPO }
    else {
        $file = Join-Path $Script:SourceDir 'setup\UPDATE_REPO'
        if (Test-Path -LiteralPath $file) { $text = [IO.File]::ReadAllText($file) }
    }
    $text = "$text".Trim()
    if ($text -and $text -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$') {
        Write-Log "Update check skipped: '$text' is not owner/name."
        return ''
    }
    return $text
}
function Get-ErrorText($Problem) {
    $e = $Problem.Exception
    while ($e.InnerException) { $e = $e.InnerException }
    return $e.Message
}
function Invoke-UpdateFetch {
    # Returns the address's text, or saves it to OutFile. Throws on anything but success.
    # HttpClient and Wait, not Invoke-WebRequest: its timeout leaves out the name lookup, and a bad network must not hold
    # this up for longer than the timeout. Waiting on the task is a hard limit, whatever the network does.
    param([string]$Url, [string]$OutFile = '', [int]$TimeoutSec = 10)
    Add-Type -AssemblyName System.Net.Http -ErrorAction Stop
    $handler = New-Object System.Net.Http.HttpClientHandler
    if ($Script:SystemProxy) { $handler.Proxy = $Script:SystemProxy }
    $client = New-Object System.Net.Http.HttpClient -ArgumentList @(, $handler)
    $limit = $TimeoutSec * 1000
    try {
        $client.Timeout = [TimeSpan]::FromSeconds($TimeoutSec)
        if (-not $OutFile) {
            $read = $client.GetStringAsync($Url)
            if (-not $read.Wait($limit)) { throw "No answer within $TimeoutSec seconds." }
            return $read.Result
        }
        $get = $client.GetAsync($Url, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead)
        if (-not $get.Wait($limit)) { throw "No answer within $TimeoutSec seconds." }
        $response = $get.Result
        [void]$response.EnsureSuccessStatusCode()
        $stream = [IO.File]::Create($OutFile)
        try {
            $copy = $response.Content.CopyToAsync($stream)
            if (-not $copy.Wait($limit)) { throw "The download did not finish within $TimeoutSec seconds." }
        } finally { $stream.Dispose() }
    } finally {
        try { $client.CancelPendingRequests() } catch { }
        try { $client.Dispose() } catch { }
    }
}
function Clear-UpdateFolder {
    Remove-Item -LiteralPath $Script:UpdateDir -Recurse -Force -ErrorAction SilentlyContinue
}
function Start-SetupInNewWindow([string]$File, [string]$UpdatedTo = '') {
    # cmd.exe reads a running batch file line by line, so this window has to be gone before anything copies over its file:
    # never run the new one nested inside it. Explorer would start it with Explorer's own environment and lose the
    # marker, so cmd.exe starts it, with the marker on its command line. Returns only if the window could not be started.
    $line = '"' + $File + '"'
    if ($UpdatedTo) { $line = 'set "HL_SETUP_UPDATED=' + $UpdatedTo + '" && ' + $line }
    else { Remove-Item Env:\HL_SETUP_UPDATED -ErrorAction SilentlyContinue }
    try {
        Start-Process -FilePath 'cmd.exe' -ArgumentList ('/d /c "' + $line + '"') -WorkingDirectory (Split-Path -Parent $File) -ErrorAction Stop | Out-Null
    } catch {
        Write-Log "Could not open a new window: $(Get-ErrorText $_)"
        return
    }
    exit 75
}
function Stop-IfOlderThanInstalled {
    # An old zip run by mistake. Works with no internet, and never copies old files over new ones.
    $mine = Get-VersionIn $Script:SourceDir
    $installed = Get-VersionIn $Script:LabDir
    if (-not (Test-NewerVersion $installed $mine)) { return }
    $file = Join-Path $Script:LabDir 'SETUP-WINDOWS.cmd'
    Write-Host ''
    Say-Warn "This file is version $mine, but version $installed is already installed, so setup will not copy old files over it."
    if (Test-Path -LiteralPath $file) {
        Say 'Carrying on with the installed copy in a new window.'
        Start-SetupInNewWindow $file
    }
    Say "Nothing was changed. To carry on, run the newest copy of this file you have."
    exit 1
}
function Start-Update([string]$Repo, [string]$Version) {
    $here = Get-VersionIn $Script:SourceDir
    $top = Join-Path $Script:UpdateDir ('{0}-main' -f ($Repo -split '/')[1])
    $file = Join-Path $top 'SETUP-WINDOWS.cmd'
    Clear-UpdateFolder
    Say 'Downloading it. This usually takes under a minute.'
    try {
        New-Item -ItemType Directory -Force -Path $Script:UpdateDir -ErrorAction Stop | Out-Null
        $zip = Join-Path $Script:UpdateDir 'update.zip'
        Invoke-UpdateFetch "https://github.com/$Repo/archive/refs/heads/main.zip" $zip 900 | Out-Null
        Expand-Archive -LiteralPath $zip -DestinationPath $Script:UpdateDir -Force -ErrorAction Stop
        Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue
        if (-not (Test-Path -LiteralPath $file) -or -not (Test-Path -LiteralPath (Join-Path $top 'docker-compose.yml'))) { throw 'The download does not hold the package.' }
        $got = Get-VersionIn $top
        if ($got -ne $Version) { throw "The download is version '$got', not $Version." }
        $body = [IO.File]::ReadAllText($file)
        if (-not $body.StartsWith('<# :')) { throw 'The setup file in the download is damaged.' }
        $null = [ScriptBlock]::Create($body)
        Get-ChildItem -LiteralPath $top -Recurse -File -Force -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue
        Set-Content -LiteralPath $Script:UpdatedToFile -Value $Version -Encoding Ascii -ErrorAction Stop
    } catch {
        Write-Log "Update failed: $(Get-ErrorText $_)"
        Clear-UpdateFolder
        Say-Warn "The update did not work, so setup is carrying on with version $here. Nothing was changed."
        return
    }
    Say "Carrying on with version $Version in a new window. This one closes by itself."
    Start-SetupInNewWindow $file $Version
    Remove-Item -LiteralPath $Script:UpdatedToFile -ErrorAction SilentlyContinue
    Clear-UpdateFolder
    Say-Warn "The update did not work, so setup is carrying on with version $here. Nothing was changed."
}
function Start-UpdateCheck {
    # The run an update started has already been through this, and it must not ask again.
    if ($env:HL_SETUP_UPDATED) { Write-Log "Running the update to $($env:HL_SETUP_UPDATED)."; return }
    # Left from the last update, unless this file is running from it.
    if (-not "$($Script:SourceDir)/".Replace('\', '/').StartsWith("$($Script:UpdateDir)/".Replace('\', '/'), [StringComparison]::OrdinalIgnoreCase)) { Clear-UpdateFolder }
    try {
        $repo = Get-UpdateRepo
        if (-not $repo) { Write-Log 'Update checks are off.'; return }
        $here = Get-VersionIn $Script:SourceDir
        $latest = "$(Invoke-UpdateFetch "https://raw.githubusercontent.com/$repo/main/setup/VERSION")".Trim()
        if (-not (Test-VersionText $latest) -or -not (Test-VersionText $here)) {
            Write-Log 'Update check skipped: the version numbers could not be read.'
            return
        }
        if (-not (Test-NewerVersion $latest $here)) { Say "You have the latest version ($here)."; return }
    } catch {
        Write-Log "Update check skipped: $(Get-ErrorText $_)"
        return
    }
    $yes = $false
    try { $yes = Ask-YesNo "Version $latest is available. Update now? Your data, settings and progress are kept." } catch { Write-Log "No answer about the update: $(Get-ErrorText $_)" }
    if (-not $yes) { Say "Carrying on with version $here."; return }
    Start-Update $repo $latest
}
function Show-UpdateResult {
    # Kept until the app really runs the new version, which is later if he chose to let a running build finish.
    if (-not (Test-Path -LiteralPath $Script:UpdatedToFile)) { return }
    $version = (Get-Content -LiteralPath $Script:UpdatedToFile -Raw).Trim()
    if (-not (Test-AppCurrent)) { return }
    Remove-Item -LiteralPath $Script:UpdatedToFile -ErrorAction SilentlyContinue
    if ($version -ne (Get-PackageVersion)) { return }
    Say-Good "Updated to $version."
    # For example: data from an earlier version needs one rebuild. That stays his decision, so it is shown and nothing more.
    $s = Invoke-Api 'GET' '/api/sim/status'
    if ($s -and $s.error) { Say-Warn "$($s.error)" }
}

# Everything setup copied last time, so a later version can remove only what it put there itself. A file he made was
# never on this list, so it can't be removed. With no list yet, nothing is removed.
function Test-PersonsOwnFile([string]$Relative) {
    return ($Relative -eq '.env' -or $Relative -eq 'landing' -or $Relative -like 'landing/*' -or $Relative -eq 'answer_key/anomalies.csv')
}
function Get-PackageFiles([string]$Dir) {
    $base = (Resolve-Path -LiteralPath $Dir).Path.TrimEnd('\', '/')
    Get-ChildItem -LiteralPath $base -Force | Where-Object { $_.Name -ne 'landing' -and $_.Name -ne '.git' } | ForEach-Object {
        if ($_.PSIsContainer) { Get-ChildItem -LiteralPath $_.FullName -Recurse -File -Force } else { $_ }
    } | ForEach-Object {
        $relative = $_.FullName.Substring($base.Length + 1) -replace '\\', '/'
        if (-not (Test-PersonsOwnFile $relative)) { $relative }
    }
}
function Remove-StaleFiles([string[]]$Current) {
    if (-not (Test-Path -LiteralPath $Script:FileList)) { return }
    # Ignoring case, like the disk: a file renamed from Foo to foo must not count as removed.
    $keep = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($f in $Current) { [void]$keep.Add($f) }
    $removed = 0
    foreach ($line in [IO.File]::ReadAllLines($Script:FileList)) {
        $relative = $line.Trim()
        if (-not $relative -or $keep.Contains($relative) -or (Test-PersonsOwnFile $relative)) { continue }
        if ($relative -match '(^|/)\.\.(/|$)|^[/\\]|:') { continue }
        $parts = @($relative -split '/')
        # Only inside folders the package owns: a file at the top of the folder stays.
        if ($parts.Count -lt 2 -or $Script:OwnedFolders -notcontains $parts[0]) { continue }
        $path = Join-Path $Script:LabDir $relative
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }
        try { Remove-Item -LiteralPath $path -Force -ErrorAction Stop } catch { Write-Log "Could not remove ${relative}: $(Get-ErrorText $_)"; continue }
        Write-Log "REMOVED $relative"
        $removed++
        for ($i = $parts.Count - 2; $i -ge 1; $i--) {
            $folder = Join-Path $Script:LabDir (($parts[0..$i]) -join '/')
            if (Get-ChildItem -LiteralPath $folder -Force -ErrorAction SilentlyContinue | Select-Object -First 1) { break }
            Remove-Item -LiteralPath $folder -Force -ErrorAction SilentlyContinue
        }
    }
    if ($removed -gt 0) { Say "Removed $removed old files this version no longer ships." }
}
function Copy-Package {
    # File by file, so a file that can't be copied says which. setup\VERSION goes last, in the step below: until it is
    # there the step reads as not done, so a copy that stopped halfway is simply done again next time.
    Say 'Copying the package.'
    $files = @(Get-PackageFiles $Script:SourceDir)
    foreach ($relative in $files) {
        if ($relative -eq 'setup/VERSION') { continue }
        $target = Join-Path $Script:LabDir $relative
        try {
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) -ErrorAction Stop | Out-Null
            Copy-Item -LiteralPath (Join-Path $Script:SourceDir $relative) -Destination $target -Force -ErrorAction Stop
        } catch {
            $Script:LastError = "Could not copy ${relative}: $(Get-ErrorText $_)"
            $Script:LastOutput = $Script:LastError
            return $false
        }
    }
    Remove-StaleFiles $files
    [IO.File]::WriteAllLines($Script:FileList, [string[]]$files)
    return $true
}

# ---------------------------------------------------------------------------------------------- hook for the tests
if ($env:HL_SETUP_STUBS) { . $env:HL_SETUP_STUBS }

# ---------------------------------------------------------------------------------------------- the steps
Write-Host ''
Write-Host '  Harborline Lab setup' -ForegroundColor White
Write-Host '  It does everything it can by itself and stops only when it needs you. Safe to run again at any time.'
Write-Host "  Log: $($Script:LogFile)"

Update-Path
if (-not (Test-Path -LiteralPath (Join-Path $Script:SourceDir 'docker-compose.yml'))) {
    Write-Host ''
    Write-Host '    This file is being run from inside the zip, or without the rest of the package.' -ForegroundColor Red
    Write-Host '    Right-click the zip, choose Extract All, open the extracted folder and run this file there.' -ForegroundColor Yellow
    exit 1
}

Stop-IfOlderThanInstalled
Start-UpdateCheck

$Script:Machine = Get-MachineInfo
$Script:VirtConfirmed = $false
$Script:DockerAlreadyWorks = Test-Docker

Step 'Check this computer' -Check {
    $m = $Script:Machine
    Say ("{0}, build {1}. {2} GB memory, {3} processors, {4} GB free." -f $m.Caption, $m.Build, $m.RamGB, $m.Cores, $m.FreeGB)
    if ($m.RamGB -lt 15) { Say-Warn 'Under 16 GB of memory: setup will build the Medium data set instead of Large.' }
    if ($m.FreeGB -lt 40) { Say-Warn 'Under 40 GB free on this drive: the Large data set may not fit. Free up space if the build fails.' }
    # Docker Desktop needs Windows 10 22H2 (19045) or Windows 11 23H2 (22631): 22000 to 22630 fall between them.
    $buildOk = $m.Build -ge 22631 -or ($m.Build -ge 19045 -and $m.Build -lt 22000)
    return (($buildOk -or $Script:DockerAlreadyWorks) -and ($m.Virt -or $Script:VirtConfirmed -or $Script:DockerAlreadyWorks))
} -Fix {
    $m = $Script:Machine
    if (-not ($m.Build -ge 22631 -or ($m.Build -ge 19045 -and $m.Build -lt 22000))) {
        Start-Process 'ms-settings:windowsupdate' -ErrorAction SilentlyContinue
        Your-Turn -Lines @('This Windows is too old for Docker Desktop. Run Windows Update until you reach Windows 10 22H2, or Windows 11 23H2 or newer.')
        $Script:Machine = Get-MachineInfo
        $Script:LastError = "Windows build $($m.Build) is too old for Docker Desktop."
    } else {
        Your-Turn -NoWait -Lines @('Open Task Manager (Ctrl+Shift+Esc), go to Performance > CPU and read Virtualization under the graph.')
        # This check is sometimes wrong on working machines, so Task Manager gets the last word.
        if (Ask-YesNo 'Does it say Enabled?') { $Script:VirtConfirmed = $true; return }
        Your-Turn -Lines @(
            'Hardware virtualization is off, and only you can turn it on:',
            'Settings > System > Recovery > Advanced startup > Restart now.',
            'Then Troubleshoot > Advanced options > UEFI Firmware Settings > Restart.',
            'Enable Intel VT-x or AMD SVM (usually under Advanced or CPU), save and exit.',
            'Windows restarts. Then run this file again.'
        )
        $Script:LastError = 'Hardware virtualization is off.'
    }
}

function Test-Winget {
    # Running it, not just finding it: a broken App Installer leaves a command that cannot start.
    if (-not (Test-Command 'winget')) { return $false }
    return (Invoke-Tool 'winget' @('--version'))
}
Step 'Windows package manager (winget)' -Check { Test-Winget } -Fix {
    try {
        $installer = Get-AppxPackage Microsoft.DesktopAppInstaller -ErrorAction Stop
        if ($installer) { Add-AppxPackage -DisableDevelopmentMode -Register (Join-Path $installer.InstallLocation 'appxmanifest.xml') -ErrorAction Stop }
        else { Add-AppxPackage -RegisterByFamilyName -MainPackage Microsoft.DesktopAppInstaller_8wekyb3d8bbwe -ErrorAction Stop }
    } catch { Write-Log "App Installer repair: $($_.Exception.Message)" }
    Update-Path
    if (-not (Test-Winget)) {
        Say 'Repairing winget from the PowerShell Gallery.'
        Invoke-Elevated 'Install-PackageProvider -Name NuGet -Force; Install-Module Microsoft.WinGet.Client -Force -Repository PSGallery; Repair-WinGetPackageManager -AllUsers' 'to repair winget' | Out-Null
        Update-Path
    }
    if (-not (Test-Winget)) {
        Start-Process 'ms-windows-store://pdp/?productid=9NBLGGH4NNS1' -ErrorAction SilentlyContinue
        Your-Turn -Lines @('The Microsoft Store is opening on App Installer. Press Get or Update.', 'No Store on this computer? Ask whoever manages it to install App Installer.')
        Update-Path
    }
    $Script:LastError = 'winget is still not available.'
}

Step 'Terminal and PowerShell 7' -Optional -Check { Test-Command 'pwsh' } -Fix {
    Install-Package -Ids @('Microsoft.PowerShell') -Label 'PowerShell 7' | Out-Null
    if (-not (Test-Command 'wt')) { Install-Package -Ids @('Microsoft.WindowsTerminal') -Label 'Windows Terminal' | Out-Null }
}

function Request-WslRestart {
    # One restart, then a report: a lasting WSL error must never become an endless restart.
    $marker = Join-Path $Script:StateDir 'wsl-restart.done'
    if (Test-Path -LiteralPath $marker) { return }
    Set-Content -LiteralPath $marker -Value (Get-Date -Format 's') -Encoding Ascii
    Request-Restart 'WSL2 is installed. Windows needs a restart to finish.'
}
Step 'WSL2 (Docker runs on it)' -Check { $Script:DockerAlreadyWorks -or (Test-WslReady) } -Fix {
    $cmd = 'wsl --install --no-distribution; if ($LASTEXITCODE -ne 0) { dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart; dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart }'
    Invoke-Elevated $cmd 'to install WSL2' | Out-Null
    if (Test-WslReady) { return }
    Request-WslRestart
    $Script:LastError = 'WSL2 is still not working after a restart.'
}

Step 'Memory limit for WSL' -Check { Test-Path -LiteralPath (Join-Path $env:USERPROFILE '.wslconfig') } -Fix {
    $m = $Script:Machine
    $gb = [math]::Max(4, [math]::Floor($m.RamGB * 2 / 3))
    $cpus = [math]::Max(2, $m.Cores - 2)
    $text = "[wsl2]`r`nmemory=${gb}GB`r`nprocessors=$cpus`r`nswap=4GB`r`n"
    [IO.File]::WriteAllText((Join-Path $env:USERPROFILE '.wslconfig'), $text, [Text.Encoding]::ASCII)
    Say "Gave WSL ${gb} GB of memory and $cpus processors."
    Stop-WslIfOnlyDocker | Out-Null
}

Step 'Docker Desktop' -Check { $Script:DockerAlreadyWorks -or [bool](Find-DockerDesktop) } -Fix {
    Install-Package -Ids @('Docker.DockerDesktop') -Label 'Docker Desktop' | Out-Null
}

Step 'Docker engine running' -Check { Test-Docker } -Fix { Start-DockerEngine }

if (Invoke-Tool 'docker' @('info', '--format', '{{.MemTotal}}')) {
    $dockerMemory = 0.0
    if ([double]::TryParse($Script:LastOutput.Trim(), [ref]$dockerMemory) -and $dockerMemory -gt 0 -and $dockerMemory -lt 8000000000) {
        Say-Warn 'Docker has under 8 GB of memory. The Large build may fail. Raise memory= in .wslconfig in your user folder, then run wsl --shutdown.'
    }
}

Step 'Git and your name on commits' -Check {
    if (-not (Test-Command 'git')) { return $false }
    # Not just the exit code: a blank name passes it, then every commit fails.
    if (-not (Invoke-Tool 'git' @('config', '--global', 'user.name')) -or -not $Script:LastOutput.Trim()) { return $false }
    return ((Invoke-Tool 'git' @('config', '--global', 'user.email')) -and $Script:LastOutput.Trim())
} -Fix {
    if (-not (Test-Command 'git')) { Install-Package -Ids @('Git.Git') -Label 'Git' | Out-Null }
    if (-not (Test-Command 'git')) { return }
    if (-not (Invoke-Tool 'git' @('config', '--global', 'user.name')) -or -not $Script:LastOutput.Trim()) {
        Your-Turn -NoWait -Lines @('Git puts your name and email on every commit.')
        $name = ''
        while (-not $name) { $name = Ask-Text 'Your full name' }
        Invoke-Tool 'git' @('config', '--global', 'user.name', $name) | Out-Null
    }
    if (-not (Invoke-Tool 'git' @('config', '--global', 'user.email')) -or -not $Script:LastOutput.Trim()) {
        $email = ''
        while ($email -notmatch '^[^@\s]+@[^@\s]+\.[^@\s]+$') { $email = Ask-Text 'Your email (the one you use on GitHub)' }
        Invoke-Tool 'git' @('config', '--global', 'user.email', $email) | Out-Null
    }
    foreach ($pair in @(@('init.defaultBranch', 'main'), @('core.autocrlf', 'true'), @('pull.ff', 'only'), @('fetch.prune', 'true'))) {
        if (-not (Invoke-Tool 'git' @('config', '--global', $pair[0]))) { Invoke-Tool 'git' @('config', '--global', $pair[0], $pair[1]) | Out-Null }
    }
}

Step 'GitHub CLI' -Check { Test-Command 'gh' } -Fix { Install-Package -Ids @('GitHub.cli') -Label 'GitHub CLI' | Out-Null }

Step 'GitHub sign-in and SSH key' -Check { Test-GitHubAccess } -Fix { Connect-GitHub }

$Script:Extensions = @('ms-python.python', 'ms-python.vscode-pylance', 'charliermarsh.ruff', 'ms-azuretools.vscode-containers', 'eamodio.gitlens', 'EditorConfig.EditorConfig', 'redhat.vscode-yaml', 'mtxr.sqltools', 'mtxr.sqltools-driver-pg', 'innoverio.vscode-dbt-power-user', 'databricks.databricks', 'snowflake.snowflake-vsc', 'mechatroner.rainbow-csv', 'GrapeCity.gc-excelviewer')
function Find-Code {
    # The real VS Code before whatever 'code' means on PATH: other editors claim that name too.
    foreach ($path in @((Join-Path $env:LOCALAPPDATA 'Programs\Microsoft VS Code\bin\code.cmd'), (Join-Path $env:ProgramFiles 'Microsoft VS Code\bin\code.cmd'))) {
        if (Test-Path -LiteralPath $path) { return $path }
    }
    if (Test-Command 'code') { return 'code' }
    return ''
}
function Get-MissingExtensions {
    $code = Find-Code
    if (-not $code -or -not (Invoke-Tool $code @('--list-extensions'))) { return $Script:Extensions }
    $have = $Script:LastOutput
    return @($Script:Extensions | Where-Object { $have -notmatch [regex]::Escape($_) })
}
Step 'VS Code and extensions' -Optional -Check { (Find-Code) -and @(Get-MissingExtensions).Count -eq 0 } -Fix {
    if (-not (Find-Code)) { Install-Package -Ids @('Microsoft.VisualStudioCode') -Label 'VS Code' | Out-Null }
    $code = Find-Code
    if (-not $code) { return }
    $failed = @()
    foreach ($ext in @(Get-MissingExtensions)) {
        Say "Adding $ext"
        if (-not (Invoke-Tool $code @('--install-extension', $ext, '--force'))) { $failed += "$ext : $($Script:LastOutput)" }
    }
    if ($failed.Count -gt 0) { $Script:LastError = "Could not add: $($failed -join '; ')" }
    if (-not (Invoke-Tool 'git' @('config', '--global', 'core.editor'))) { Invoke-Tool 'git' @('config', '--global', 'core.editor', 'code --wait') | Out-Null }
}

Step 'uv, Python 3.12 and pre-commit' -Check {
    if (-not (Test-Command 'uv') -or -not (Test-Command 'pre-commit')) { return $false }
    return (Invoke-Tool 'uv' @('python', 'find', '3.12'))
} -Fix {
    if (-not (Test-Command 'uv')) { Install-Package -Ids @('astral-sh.uv') -Label 'uv' | Out-Null }
    if (-not (Test-Command 'uv')) { return }
    # Stop at the first failure, so the report shows the real error and not the last command's.
    $Script:InstalledSomething = $true
    if (-not (Invoke-Tool 'uv' @('python', 'install', '3.12') -Live)) { $Script:LastError = $Script:LastOutput; return }
    if (-not (Invoke-Tool 'uv' @('tool', 'install', 'pre-commit') -Live)) { $Script:LastError = $Script:LastOutput; return }
    # Known problem: uv's tools folder is not on PATH, so 'pre-commit' is "not recognized".
    Invoke-Tool 'uv' @('tool', 'update-shell') | Out-Null
    Update-Path
}

Step 'DBeaver (database client)' -Optional -Check { [bool](Find-DBeaver) } -Fix {
    Install-Package -Ids @('DBeaver.DBeaver.Community') -Label 'DBeaver' | Out-Null
}

Step "Simulator in $($Script:LabDir)" -Check {
    if (-not (Test-Path -LiteralPath (Join-Path $Script:LabDir 'docker-compose.yml'))) { return $false }
    $here = Join-Path $Script:SourceDir 'setup\VERSION'
    $there = Join-Path $Script:LabDir 'setup\VERSION'
    return ((Test-Path -LiteralPath $there) -and ((Get-Content -LiteralPath $here -Raw) -eq (Get-Content -LiteralPath $there -Raw)))
} -Fix {
    New-Item -ItemType Directory -Force -Path $Script:LabDir | Out-Null
    $elsewhere = (Resolve-Path -LiteralPath $Script:SourceDir).Path -ne (Resolve-Path -LiteralPath $Script:LabDir).Path
    if ($elsewhere -and -not (Copy-Package)) { return }
    New-Item -ItemType Directory -Force -Path (Join-Path $Script:LabDir 'landing') | Out-Null
    Get-ChildItem -LiteralPath $Script:LabDir -ErrorAction SilentlyContinue | Where-Object { $_.Name -ne 'landing' } | ForEach-Object {
        if ($_.PSIsContainer) { Get-ChildItem -LiteralPath $_.FullName -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue }
        else { Unblock-File -LiteralPath $_.FullName -ErrorAction SilentlyContinue }
    }
    # Scripts that run inside Linux containers break with Windows line endings, whatever unpacked the zip.
    Get-ChildItem -LiteralPath (Join-Path $Script:LabDir 'db\init') -File -ErrorAction SilentlyContinue | ForEach-Object {
        $text = [IO.File]::ReadAllText($_.FullName)
        if ($text.Contains("`r`n")) { [IO.File]::WriteAllText($_.FullName, $text.Replace("`r`n", "`n"), (New-Object Text.UTF8Encoding($false))) }
    }
    if ($elsewhere) { Copy-Item -LiteralPath (Join-Path $Script:SourceDir 'setup\VERSION') -Destination (Join-Path $Script:LabDir 'setup\VERSION') -Force }
}

Step 'Settings and free ports' -Check {
    if (@(Get-MissingKeys).Count -gt 0) { return $false }
    # Each run, so it follows ports he changed, and an install from before there was a backup gets one.
    Save-EnvBackup
    if ((Get-ProjectContainers $Script:OldProject).Count -gt 0) { return $false }
    if ((Get-ProjectContainers $Script:Project).Count -gt 0) { return $true }
    return ((Test-PortFree ([int](Get-PgPort))) -and (Test-PortFree ([int](Get-AppPort))))
} -Fix {
    Repair-EnvFile
    $old = Get-ProjectContainers $Script:OldProject -All
    if ($old.Count -gt 0) {
        Say 'Stopping the containers from the earlier attempt. Its data stays on disk, untouched.'
        Invoke-Tool 'docker' (@('rm', '-f') + $old) | Out-Null
    }
    Move-BusyPorts | Out-Null
}

Step 'Start the simulator' -Check {
    if ((Get-ProjectContainers $Script:Project).Count -lt 2) { return $false }
    if (-not $Script:KeepOldApp -and -not (Test-AppCurrent)) { return $false }
    # Slow is not down. Restarting here would stop a data build that is running fine.
    return (Wait-Health 300)
} -Fix {
    if ((Get-ProjectContainers $Script:Project).Count -ge 2 -and -not (Test-AppCurrent)) {
        $running = Get-RunningBuild
        if ($running) {
            Your-Turn -NoWait -Lines @(
                "A data build is running: $running.",
                'This version needs the app restarted, which stops that build and loses its progress.',
                '  K  Keep it running. You stay on the old version until it finishes. Run this file again after that.',
                '  R  Restart now on this version, then choose a data size and build again.')
            $answer = ''
            while ($answer -notmatch '^[KkRr]$') { $answer = Ask-Text 'Type K or R' 'K' }
            if ($answer -match '^[Kk]$') { $Script:KeepOldApp = $true; return }
        }
    }
    Say 'Building and starting the containers (several minutes the first time).'
    Push-Location -LiteralPath $Script:LabDir
    try {
        $up = Invoke-Tool 'docker' @('compose', 'up', '-d', '--build') -Live
        if (-not $up) {
            # What compose printed, plus the database's own log, which often holds the real reason.
            $composeOutput = $Script:LastOutput
            $composeCommand = $Script:LastCommand
            $composeCode = $Script:LastCode
            Invoke-Tool 'docker' @('compose', 'logs', '--tail', '40', 'postgres') | Out-Null
            $Script:LastOutput = "$composeOutput`n--- postgres log ---`n$($Script:LastOutput)"
            $Script:LastCommand = $composeCommand
            $Script:LastCode = $composeCode
        }
    } finally { Pop-Location }
    if ($up) {
        Set-Content -LiteralPath (Join-Path $Script:StateDir 'app-version.txt') -Value (Get-PackageVersion)
        Say 'Waiting for the app to answer.'
        if (-not (Wait-Health 300)) {
            Push-Location -LiteralPath $Script:LabDir
            try { Invoke-Tool 'docker' @('compose', 'logs', '--tail', '60', 'app') | Out-Null } finally { Pop-Location }
            $Script:LastError = 'The app did not answer within 5 minutes.'
        }
    }
}

# built_at is written only after a build has finished every step and checked its data, and it is cleared before a new
# build wipes anything. It lives in the database, next to the data it describes. Nothing else counts as finished.
Step 'Start building the data' -Check {
    $Script:BuildStatus = Get-SimStatus
    $s = $Script:BuildStatus
    if (-not $s) { return $false }
    if ($s.state -eq 'building') {
        Say "A $($s.preset) build is running. To pick another size, press Cancel on the Control Center and run this file again."
        return $true
    }
    if ($s.built_at) { Say "Your $($s.preset) data is already built. Setup will not touch it."; return $true }
    return $false
} -Fix {
    if (-not $Script:BuildStatus) { $Script:LastError = $Script:NoAnswerText; return }
    Start-DataBuild
}

Step 'Tracker start date' -Check {
    $m = Invoke-Api 'GET' '/api/tracker/meta'
    return ($m -and $m.configured)
} -Fix {
    $today = (Get-Date).Date
    $next = $today.AddDays(((8 - [int]$today.DayOfWeek) % 7))
    $default = $next.ToString('yyyy-MM-dd')
    Your-Turn -NoWait -Lines @('Every due date in the eight projects counts from your start date.')
    $date = ''
    while ($date -notmatch '^\d{4}-\d{2}-\d{2}$') { $date = Ask-Text 'The Monday you start, as YYYY-MM-DD (Enter accepts this one)' $default }
    $null = Invoke-Api 'POST' '/api/tracker/setup' @{ start_date = $date }
    $m = Invoke-Api 'GET' '/api/tracker/meta'
    if ($m -and $m.sprints) {
        $first = @($m.sprints)[0]
        if ($first.state -eq 'future') { $null = Invoke-Api 'POST' "/api/tracker/sprints/$($first.id)/start" }
    }
}

Step "Your repository $($Script:RepoName)" -Check { Test-RepoReady } -Fix { New-AnalyticsRepo }

Step 'Commit checks in your repository' -Optional -Check { Test-RepoHooks } -Fix { Install-RepoHooks }

Step 'Protect main on GitHub' -Optional -Check { Test-RepoProtected } -Fix {
    $user = Get-GitHubUser
    Invoke-Tool 'gh' @('api', '--method', 'POST', "repos/$user/$($Script:RepoName)/rulesets", '--input', (Join-Path $Script:LabDir 'setup\repo-ruleset.json')) | Out-Null
}

Step 'DBeaver connections' -Optional -Check { Test-DBeaverConnections } -Fix { Add-DBeaverConnections }

# -NoFreshWindow: stopping here is his choice, not a failure a new window could cure.
Step 'Your team chats on your AI site' -Optional -NoFreshWindow -Check { Test-TeamChats } -Fix { Start-TeamChats }

# ---------------------------------------------------------------------------------------------- the data build
function Watch-Build {
    Write-Host ''
    Write-Host '  Everything is set up. The data build is the last thing running.' -ForegroundColor White
    Write-Host '  You can close this window at any time: the build keeps going by itself.'
    $last = ''
    $silent = 0
    while ($true) {
        $s = Invoke-Api 'GET' '/api/sim/status'
        if (-not $s) {
            # Don't wait forever on an app that has gone away.
            $silent++
            if ($silent -ge 20) { return [pscustomobject]@{ state = 'silent'; built_at = $null } }
            Start-Sleep -Seconds 10
            continue
        }
        $silent = 0
        if ($s.state -ne 'building') { return $s }
        $b = $s.build
        if ($b) {
            $eta = if ($b.eta_s) { ", about $([math]::Ceiling($b.eta_s / 60)) min left" } else { '' }
            $line = "  Data build: $([math]::Floor([double]$b.progress * 100))% ($($b.phase))$eta"
            if ($line -ne $last) { Write-Host $line; $last = $line }
        }
        Start-Sleep -Seconds 15
    }
}

$Script:PassText = 'Finished.'
Step 'Data build finished' -Check {
    $s = Watch-Build
    $Script:BuildFailed = $false
    if ($s.built_at) { return $true }
    if ($s.state -eq 'silent') {
        $Script:LastError = 'The app stopped answering for 10 minutes. Nothing was wiped, and the build may still be running.'
        $Script:LastOutput = $Script:LastError
    } else {
        # It answered, and the data has no finished stamp: stopped, cancelled or failed.
        $Script:BuildFailed = $true
        if ($s.last_run -and $s.last_run.status -eq 'cancelled') {
            $Script:BuildWasCancelled = $true
            $Script:Preset = $null
            $Script:LastError = 'The data build was cancelled.'
            return $false
        }
        $Script:BuildFailure = "The data build did not finish: $($s.error)"
        $Script:LastError = $Script:BuildFailure
        $Script:LastOutput = "$($s.error)"
    }
    Push-Location -LiteralPath $Script:LabDir
    try {
        $reason = $Script:LastOutput
        Invoke-Tool 'docker' @('compose', 'logs', '--tail', '2000', 'app') | Out-Null
        $log = @($Script:LastOutput -split "`n" | Where-Object { $_ -notmatch 'GET /api/health' } | Select-Object -Last 40) -join "`n"
        $Script:LastOutput = "$reason`n--- app log ---`n$log"
    } finally { Pop-Location }
    return $false
} -Fix {
    if (-not $Script:BuildFailed) { return }
    if (-not $Script:BuildWasCancelled) { Say "$($Script:BuildFailure) Starting it again." }
    $Script:LastOutput = ''
    Start-DataBuild
    $Script:BuildWasCancelled = $false
}

# ---------------------------------------------------------------------------------------------- finish
$url = Get-AppUrl
$pgPort = Get-PgPort
Remove-Item -LiteralPath $Script:ErrorFile -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $Script:StateDir 'fresh-window.done') -ErrorAction SilentlyContinue
Write-Host ''
Write-Host '  ================================ ALL DONE ================================' -ForegroundColor Green
foreach ($r in $Script:Results) {
    if ($r.Status -eq 'needs a retry') { Write-Host ("    {0,-14} {1}" -f $r.Status, $r.Step) -ForegroundColor Yellow }
}
$fresh = @($Script:Results | Where-Object { $_.Status -eq 'done' }).Count
$kept = @($Script:Results | Where-Object { $_.Status -eq 'skipped' }).Count
Write-Host "    $fresh steps done now, $kept were already in place."
Show-UpdateResult
Write-Host ''
Write-Host "    Simulator      $($Script:LabDir)"
Write-Host "    Your repo      $($Script:RepoDir)"
Write-Host "    Control Center $url"
Write-Host "    Team           $url/team"
Write-Host "    Chat zip       $(Join-Path $Script:Root 'harborline-chat.zip') (attach it with every prompt)"
Write-Host "    Tracker        $url/tracker"
Write-Host "    Databases      localhost:$pgPort, login etl_reader, password HL_ETL_PASSWORD in .env"
Write-Host '                   wms (schema wms), ax2012 (schema dbo), iot (schema telemetry), tracker (schema tracker)'
Write-Host ''
Write-Host '    Next, by hand:' -ForegroundColor Yellow
Write-Host '    1. Close any terminal or VS Code window you had open, and open a new one. Old windows cannot see what was just installed.' -ForegroundColor Yellow
Write-Host '    2. Read the project brief, then sign up for your cloud platform.' -ForegroundColor Yellow
Write-Host '    3. Anyone you skipped in the team chats: run this file again.' -ForegroundColor Yellow
if ($Script:KeepOldApp) { Write-Host '    4. You kept the old version running so the build could finish. Run this file again to update.' -ForegroundColor Yellow }
$left = @($Script:OldFolders | ForEach-Object { Join-Path $Script:Root $_ } | Where-Object { Test-Path -LiteralPath $_ })
if ($left.Count -gt 0) {
    Write-Host ''
    Write-Host '    Left over from the earlier attempt, unused and safe to delete when you are happy:'
    foreach ($f in $left) { Write-Host "      $f" }
    Write-Host "      Docker data: docker volume rm $($Script:OldProject)_pgdata"
    Write-Host '      GitHub repo: harborline-data-platform, if you made one'
}
Write-Host ''
try { Start-Process "$url/START_HERE.html" } catch { }
exit 0
