<#
.SYNOPSIS
    Printer Nanny — site agent installer for Windows Server / Windows 10+.

.DESCRIPTION
    Installs the agent into a venv and registers it as a Windows service via NSSM.
    Designed to be piped from the central server with the enrollment values baked in:

        iwr -useb https://CENTRAL/install-agent.ps1 | iex

    when run with the env vars PN_CENTRAL_URL / PN_AGENT_ID / PN_API_KEY set, or
    via a downloaded file:

        .\install-agent.ps1 -CentralUrl https://CENTRAL -AgentId 12 -ApiKey pn_xxxxx

    Re-running upgrades in place. Subnets, SNMP, and intervals come from the central
    UI — they are NOT configured here.

.PARAMETER Uninstall
    Stop the service, remove it, and delete install and config directories.

.NOTES
    Requires:
      * Administrator (the script self-checks and aborts otherwise)
      * Python 3.10+ on PATH (or accessible via `py -3`)
        winget install Python.Python.3.12 -e --silent
      * Outbound HTTPS to the central server and to nssm.cc (once, to download NSSM)
#>
[CmdletBinding()]
param(
    [string]$CentralUrl = $env:PN_CENTRAL_URL,
    [Nullable[int]]$AgentId = $(if ($env:PN_AGENT_ID) { [int]$env:PN_AGENT_ID } else { $null }),
    [string]$ApiKey = $env:PN_API_KEY,
    [string]$PipSource = $(if ($env:PN_PIP_SOURCE) { $env:PN_PIP_SOURCE } else { "git+https://github.com/your-org/printer-nanny.git#subdirectory=agent" }),
    [string]$InstallDir = "$env:ProgramData\PrinterNanny\agent",
    [string]$ConfigDir = "$env:ProgramData\PrinterNanny",
    [string]$ServiceName = "PrinterNannyAgent",
    [switch]$NoVerifyTls,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Admin)) {
    throw "must run as Administrator (right-click PowerShell -> Run as administrator)"
}

if ($Uninstall) {
    Write-Host "==> uninstalling Printer Nanny agent"
    $nssm = Join-Path $InstallDir "nssm.exe"
    if (Test-Path $nssm) {
        & $nssm stop $ServiceName confirm 2>$null | Out-Null
        & $nssm remove $ServiceName confirm 2>$null | Out-Null
    } else {
        sc.exe stop $ServiceName 2>$null | Out-Null
        sc.exe delete $ServiceName 2>$null | Out-Null
    }
    Remove-Item -Recurse -Force $InstallDir -ErrorAction SilentlyContinue
    Remove-Item -Force (Join-Path $ConfigDir "agent.toml") -ErrorAction SilentlyContinue
    Write-Host "==> done"
    return
}

# Required args
foreach ($name in @('CentralUrl','AgentId','ApiKey')) {
    $v = Get-Variable $name -ValueOnly -ErrorAction SilentlyContinue
    if ($null -eq $v -or $v -eq '') {
        throw "missing -$name (or set environment variable PN_$($name.ToUpper().Replace('CENTRALURL','CENTRAL_URL').Replace('AGENTID','AGENT_ID').Replace('APIKEY','API_KEY')))"
    }
}
if ($PipSource -like '*your-org*') {
    throw "PipSource still points at the 'your-org' placeholder. Pass -PipSource with your real repo, or set it in the central UI (Settings -> Agent install)."
}

Write-Host "==> Printer Nanny agent installer"
Write-Host "    central : $CentralUrl"
Write-Host "    agent   : #$AgentId"
Write-Host "    install : $InstallDir"

# --- Locate Python 3.10+ ---
function Find-Python {
    foreach ($candidate in @(@('py','-3'), @('python'), @('python3'))) {
        try {
            $verRaw = & $candidate[0] @($candidate[1..($candidate.Length-1)]) --version 2>&1
            if ($verRaw -match 'Python\s+(\d+)\.(\d+)') {
                $maj = [int]$Matches[1]; $min = [int]$Matches[2]
                if ($maj -gt 3 -or ($maj -eq 3 -and $min -ge 10)) {
                    return @{ Cmd = $candidate; Version = "$maj.$min" }
                }
            }
        } catch { continue }
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    throw @"
Python 3.10+ is required and was not found.
Install it then re-run this script:

  winget install Python.Python.3.12 -e --silent

or download from https://www.python.org/downloads/windows/ — make sure
'Add Python to PATH' is checked during install.
"@
}
Write-Host "==> using Python $($py.Version): $($py.Cmd -join ' ')"

# --- Create install dir + venv ---
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null

$venv = Join-Path $InstallDir ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    if (Test-Path $venv) {
        Write-Host "    .venv exists but python is missing — recreating"
        Remove-Item -Recurse -Force $venv
    }
    & $py.Cmd[0] @($py.Cmd[1..($py.Cmd.Length-1)]) -m venv $venv
}

$pip = Join-Path $venv "Scripts\pip.exe"
$agentExe = Join-Path $venv "Scripts\printer-nanny-agent.exe"

Write-Host "==> installing printer-nanny-agent (pip source: $PipSource)"
& $pip install --quiet --upgrade pip
# --force-reinstall on the agent itself so a same-version upgrade still replaces
# code on disk; --no-deps keeps httpx/pysnmp from being rebuilt each time.
& $pip install --quiet --upgrade --force-reinstall --no-deps $PipSource
# Re-install once more without force, to pull deps if they were missing on first run.
& $pip install --quiet --upgrade $PipSource

# --- Write config ---
$cfgPath = Join-Path $ConfigDir "agent.toml"
$tlsLine = if ($NoVerifyTls) { 'verify_tls = false' } else { 'verify_tls = true' }
$cfg = @"
# Generated by install-agent.ps1 — operational settings are managed in the central UI.
central_url = "$CentralUrl"
agent_id = $AgentId
api_key = "$ApiKey"
$tlsLine
"@
Set-Content -Path $cfgPath -Value $cfg -Encoding UTF8

# Restrict config to SYSTEM + Administrators only (api_key is a secret).
$acl = Get-Acl $cfgPath
$acl.SetAccessRuleProtection($true, $false)
$systemRule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    "NT AUTHORITY\SYSTEM", "FullControl", "Allow")
$adminRule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    "BUILTIN\Administrators", "FullControl", "Allow")
$acl.SetAccessRule($systemRule)
$acl.SetAccessRule($adminRule)
Set-Acl -Path $cfgPath -AclObject $acl
Write-Host "==> wrote $cfgPath (restricted to SYSTEM + Administrators)"

# --- Ensure NSSM is present ---
$nssm = Join-Path $InstallDir "nssm.exe"
if (-not (Test-Path $nssm)) {
    Write-Host "==> downloading NSSM (Windows service wrapper) ..."
    $nssmZip = Join-Path $env:TEMP "nssm-2.24.zip"
    $nssmDir = Join-Path $env:TEMP "nssm-pn"
    Invoke-WebRequest -Uri "https://nssm.cc/release/nssm-2.24.zip" -OutFile $nssmZip -UseBasicParsing
    if (Test-Path $nssmDir) { Remove-Item -Recurse -Force $nssmDir }
    Expand-Archive -Path $nssmZip -DestinationPath $nssmDir -Force
    $arch = if ([Environment]::Is64BitOperatingSystem) { "win64" } else { "win32" }
    Copy-Item (Join-Path $nssmDir "nssm-2.24\$arch\nssm.exe") $nssm -Force
    Remove-Item -Recurse -Force $nssmDir, $nssmZip
}

# --- Run selftest before registering the service ---
Write-Host "==> running selftest"
$env:PRINTER_NANNY_CONFIG = $cfgPath
& $agentExe --config $cfgPath selftest
if ($LASTEXITCODE -ne 0) {
    throw "selftest failed (exit $LASTEXITCODE) — check central URL / agent ID / API key / TLS"
}

# --- (Re)register service ---
if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    Write-Host "==> stopping existing service for upgrade"
    & $nssm stop $ServiceName confirm | Out-Null
    & $nssm remove $ServiceName confirm | Out-Null
}

Write-Host "==> installing service '$ServiceName'"
& $nssm install $ServiceName $agentExe run | Out-Null
& $nssm set $ServiceName AppDirectory $InstallDir | Out-Null
& $nssm set $ServiceName AppEnvironmentExtra "PRINTER_NANNY_CONFIG=$cfgPath" | Out-Null
& $nssm set $ServiceName Description "Printer Nanny site agent" | Out-Null
& $nssm set $ServiceName Start SERVICE_AUTO_START | Out-Null
& $nssm set $ServiceName AppStdout (Join-Path $InstallDir "agent.log") | Out-Null
& $nssm set $ServiceName AppStderr (Join-Path $InstallDir "agent.log") | Out-Null
& $nssm set $ServiceName AppRotateFiles 1 | Out-Null
& $nssm set $ServiceName AppRotateBytes 5242880 | Out-Null
& $nssm set $ServiceName AppRestartDelay 10000 | Out-Null

& $nssm start $ServiceName | Out-Null
Start-Sleep -Seconds 2
$svc = Get-Service -Name $ServiceName
Write-Host ""
Write-Host "==> done. Service: $ServiceName ($($svc.Status))"
Write-Host "    Logs   : $InstallDir\agent.log"
Write-Host "    Config : $cfgPath"
Write-Host "    Status : Get-Service $ServiceName"
Write-Host "    Probe  : & '$agentExe' --config '$cfgPath' probe <printer-ip>"
