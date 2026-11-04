<#
  Harborline 3PL Simulator - prerequisite check for Windows. It only looks; it never installs or changes anything.
  Run from the simulator folder:
      powershell -ExecutionPolicy Bypass -File .\doctor.ps1
#>
$ErrorActionPreference = 'SilentlyContinue'
$script:fails = 0
$script:warns = 0

function Report([string]$level, [string]$check, [string]$detail, [string]$fix) {
    $color = @{ PASS = 'Green'; WARN = 'Yellow'; FAIL = 'Red'; INFO = 'Cyan' }[$level]
    Write-Host ("[{0}] " -f $level) -ForegroundColor $color -NoNewline
    Write-Host ("{0}: {1}" -f $check, $detail)
    if ($fix -and $level -ne 'PASS') { Write-Host ("       fix: {0}" -f $fix) -ForegroundColor DarkGray }
    if ($level -eq 'FAIL') { $script:fails++ }
    if ($level -eq 'WARN') { $script:warns++ }
}

function Read-EnvValue([string]$name, [string]$default) {
    foreach ($file in @('.env', '.env.example')) {
        $path = Join-Path $PSScriptRoot $file
        if (Test-Path $path) {
            foreach ($line in Get-Content $path) {
                if ($line -match ('^\s*' + [regex]::Escape($name) + '\s*=\s*(.+?)\s*$')) { return $Matches[1] }
            }
        }
    }
    return $default
}

Write-Host ''
Write-Host 'Harborline 3PL Simulator - doctor' -ForegroundColor Cyan
Write-Host '---------------------------------'

# 1. Windows version (WSL2 + Docker Desktop need Windows 10 22H2 / build 19045 or Windows 11)
$os = Get-CimInstance Win32_OperatingSystem
$build = [int]$os.BuildNumber
if ($build -ge 22000) { Report 'PASS' 'Windows' ("{0} (build {1})" -f $os.Caption, $build) '' }
elseif ($build -ge 19045) { Report 'PASS' 'Windows' ("{0} (build {1})" -f $os.Caption, $build) '' }
elseif ($build -ge 19041) { Report 'WARN' 'Windows' ("{0} (build {1})" -f $os.Caption, $build) 'Install the latest Windows 10 feature update (22H2) from Windows Update.' }
else { Report 'FAIL' 'Windows' ("{0} (build {1}) is too old for WSL2" -f $os.Caption, $build) 'Upgrade to Windows 10 22H2 or Windows 11.' }

# 2. Memory and CPU
$ramGb = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
if ($ramGb -ge 15) { Report 'PASS' 'RAM' ("{0} GB" -f $ramGb) '' }
elseif ($ramGb -ge 8) { Report 'WARN' 'RAM' ("{0} GB (16 GB recommended)" -f $ramGb) 'Use the Medium preset and close other heavy apps while building.' }
else { Report 'FAIL' 'RAM' ("{0} GB" -f $ramGb) 'The simulator needs a machine with at least 8 GB (16 GB recommended).' }
$cpus = (Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors
if ($cpus -ge 4) { Report 'PASS' 'CPU' ("{0} logical processors" -f $cpus) '' }
else { Report 'WARN' 'CPU' ("{0} logical processors" -f $cpus) 'Builds will be slow; prefer the Small or Medium preset.' }

# 3. Virtualization
$cs = Get-CimInstance Win32_ComputerSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
if ($cs.HypervisorPresent) { Report 'PASS' 'Virtualization' 'a hypervisor is running (WSL2 / Hyper-V available)' '' }
elseif ($cpu.VirtualizationFirmwareEnabled) { Report 'PASS' 'Virtualization' 'enabled in firmware' '' }
else { Report 'FAIL' 'Virtualization' 'not enabled' 'Enable Intel VT-x / AMD-V (SVM) in the BIOS/UEFI settings, then run: wsl --install' }

# 4. WSL2
$env:WSL_UTF8 = '1'
$wslStatus = (& wsl.exe --status 2>&1) -join ' '
if ($LASTEXITCODE -eq 0 -and $wslStatus -match '2') { Report 'PASS' 'WSL' 'installed (default version 2)' '' }
elseif ($LASTEXITCODE -eq 0) { Report 'WARN' 'WSL' 'installed, but the default version may not be 2' 'Run: wsl --set-default-version 2' }
else { Report 'FAIL' 'WSL' 'not installed' 'Run in an admin terminal: wsl --install   then restart Windows.' }

# 5. Docker Desktop
$dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
if (-not $dockerCmd) {
    Report 'FAIL' 'Docker' 'docker command not found' 'Install Docker Desktop: winget install -e --id Docker.DockerDesktop'
} else {
    $server = (& docker info --format '{{.ServerVersion}}' 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $server) {
        Report 'FAIL' 'Docker engine' 'Docker Desktop is not running' 'Start Docker Desktop and wait for "Engine running".'
    } else {
        Report 'PASS' 'Docker engine' ("running (server {0})" -f $server) ''
        $memBytes = [double](& docker info --format '{{.MemTotal}}' 2>$null)
        $memGb = [math]::Round($memBytes / 1GB, 1)
        if ($memGb -ge 8) { Report 'PASS' 'Docker memory' ("{0} GB available to containers" -f $memGb) '' }
        elseif ($memGb -ge 6) { Report 'WARN' 'Docker memory' ("{0} GB (8 GB or more recommended)" -f $memGb) 'Give WSL more memory: in %UserProfile%\.wslconfig set [wsl2] memory=10GB, then run: wsl --shutdown' }
        else { Report 'FAIL' 'Docker memory' ("{0} GB" -f $memGb) 'Give WSL at least 8 GB: in %UserProfile%\.wslconfig set [wsl2] memory=10GB, then run: wsl --shutdown' }
        $os2 = (& docker info --format '{{.OSType}}' 2>$null)
        if ($os2 -eq 'linux') { Report 'PASS' 'Docker mode' 'Linux containers' '' }
        else { Report 'FAIL' 'Docker mode' ("{0} containers" -f $os2) 'Right-click the Docker tray icon and choose "Switch to Linux containers".' }
    }
    $compose = (& docker compose version --short 2>$null)
    if ($LASTEXITCODE -eq 0 -and $compose) { Report 'PASS' 'Docker Compose' ("v{0}" -f $compose.TrimStart('v')) '' }
    else { Report 'FAIL' 'Docker Compose' 'docker compose (v2) not available' 'Update Docker Desktop to the latest version.' }
}

# 6. Free disk space (the Large build needs about 15 GB for Postgres plus room for landing files)
$drive = (Get-Item $PSScriptRoot).PSDrive
$freeGb = [math]::Round($drive.Free / 1GB, 1)
if ($freeGb -ge 25) { Report 'PASS' 'Disk' ("{0} GB free on {1}:" -f $freeGb, $drive.Name) '' }
elseif ($freeGb -ge 15) { Report 'WARN' 'Disk' ("{0} GB free on {1}: (25 GB recommended)" -f $freeGb, $drive.Name) 'Free some space or use the Medium preset.' }
else { Report 'FAIL' 'Disk' ("{0} GB free on {1}:" -f $freeGb, $drive.Name) 'Free at least 25 GB (Docker images, database and landing files).' }
if ($drive.Name -ne 'C') {
    $c = Get-PSDrive C
    $cFree = [math]::Round($c.Free / 1GB, 1)
    if ($cFree -lt 20) { Report 'WARN' 'Disk C:' ("{0} GB free (Docker Desktop keeps its data on C: by default)" -f $cFree) 'Free space on C: or move the Docker disk image (Docker Desktop > Settings > Resources > Advanced).' }
}

# 7. Ports
$ownContainers = (& docker ps --filter 'name=harborline' --format '{{.Names}}' 2>$null) -join ' '
foreach ($p in @(@{ Name = 'Postgres port'; Port = [int](Read-EnvValue 'HL_PG_PORT' '5432'); Var = 'HL_PG_PORT' },
                 @{ Name = 'App port'; Port = [int](Read-EnvValue 'HL_APP_PORT' '8080'); Var = 'HL_APP_PORT' })) {
    $listener = Get-NetTCPConnection -State Listen -LocalPort $p.Port -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $listener) { Report 'PASS' $p.Name ("{0} is free" -f $p.Port) '' }
    else {
        $proc = (Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue).ProcessName
        if ($ownContainers -and $proc -match 'docker|wslrelay|vpnkit|com.docker') { Report 'PASS' $p.Name ("{0} is used by Harborline itself" -f $p.Port) '' }
        else { Report 'FAIL' $p.Name ("{0} is in use by {1}" -f $p.Port, $proc) ("Stop that program, or set {0} to another port in .env" -f $p.Var) }
    }
}

# 8. Git
$git = Get-Command git -ErrorAction SilentlyContinue
if ($git) {
    Report 'PASS' 'Git' ((& git --version) -replace 'git version ', 'version ') ''
    $autocrlf = (& git config --global core.autocrlf 2>$null)
    if ($autocrlf -eq 'true' -or $autocrlf -eq 'input') { Report 'PASS' 'Git line endings' ("core.autocrlf={0}" -f $autocrlf) '' }
    else { Report 'WARN' 'Git line endings' 'core.autocrlf is not set' 'Run: git config --global core.autocrlf true' }
} else {
    Report 'FAIL' 'Git' 'not installed' 'Install Git for Windows: winget install -e --id Git.Git'
}

# 9. Simulator files
foreach ($f in @('docker-compose.yml', '.env.example', 'db\init\00_roles_and_databases.sh', 'simulator\Dockerfile')) {
    if (-not (Test-Path (Join-Path $PSScriptRoot $f))) { Report 'FAIL' 'Files' ("{0} is missing" -f $f) 'Unzip the whole simulator folder again.' }
}
if (Test-Path (Join-Path $PSScriptRoot '.env')) { Report 'PASS' 'Settings' '.env exists' '' }
else { Report 'INFO' 'Settings' '.env not created yet (start.bat copies .env.example)' '' }

Write-Host ''
if ($script:fails -gt 0) {
    Write-Host ("{0} problem(s) to fix, {1} warning(s). Fix the FAIL lines, then run doctor.ps1 again." -f $script:fails, $script:warns) -ForegroundColor Red
    exit 1
}
if ($script:warns -gt 0) { Write-Host ("Ready, with {0} warning(s). You can run start.bat." -f $script:warns) -ForegroundColor Yellow }
else { Write-Host 'All checks passed. Run start.bat.' -ForegroundColor Green }
exit 0
