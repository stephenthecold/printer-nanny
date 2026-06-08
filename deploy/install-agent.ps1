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
    [string]$PythonExe = $env:PN_PYTHON_EXE,
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
# winget's --silent install doesn't refresh PATH in the current PowerShell
# session, so probing `python` / `py` first will miss a perfectly good install.
# PEP 514 says every Python registers itself in HKLM\SOFTWARE\Python\PythonCore
# (or HKCU/WOW6432Node for per-user / 32-bit installs); that's the canonical
# lookup and works regardless of PATH. Common install dirs are a final fallback.
#
# Returns a hashtable @{ Exe = "<path or command>"; PreArgs = @(...); Version = "X.Y" }.
# PreArgs is empty for direct python.exe paths and @('-3') for the py launcher.
function _ProbeVersion([string]$exe, [string[]]$preArgs) {
    try {
        $verRaw = & $exe @preArgs --version 2>&1
        if ($verRaw -match 'Python\s+(\d+)\.(\d+)') {
            $maj = [int]$Matches[1]; $min = [int]$Matches[2]
            if ($maj -gt 3 -or ($maj -eq 3 -and $min -ge 10)) {
                return @{ Exe = $exe; PreArgs = $preArgs; Version = "$maj.$min" }
            }
        }
    } catch { }
    return $null
}

function Find-Python {
    # Tier 1: PATH (cheapest when it works).
    foreach ($entry in @(
        @{ Exe = 'py'; PreArgs = @('-3') },
        @{ Exe = 'python'; PreArgs = @() },
        @{ Exe = 'python3'; PreArgs = @() }
    )) {
        $hit = _ProbeVersion $entry.Exe $entry.PreArgs
        if ($hit) { return $hit }
    }
    # Tier 2: PEP 514 registry lookup — newest version first.
    $regBases = @(
        "HKLM:\SOFTWARE\Python\PythonCore",
        "HKCU:\SOFTWARE\Python\PythonCore",
        "HKLM:\SOFTWARE\WOW6432Node\Python\PythonCore"
    )
    $found = @()
    foreach ($regBase in $regBases) {
        if (-not (Test-Path $regBase)) { continue }
        foreach ($sub in (Get-ChildItem $regBase -ErrorAction SilentlyContinue)) {
            $verKey = $sub.PSChildName
            if ($verKey -notmatch '^(\d+)\.(\d+)$') { continue }
            $maj = [int]$Matches[1]; $min = [int]$Matches[2]
            if ($maj -lt 3 -or ($maj -eq 3 -and $min -lt 10)) { continue }
            $ipKey = "$regBase\$verKey\InstallPath"
            $installPath = (Get-ItemProperty -Path $ipKey -Name "(default)" -ErrorAction SilentlyContinue)."(default)"
            if ($installPath) {
                $pyExe = Join-Path $installPath "python.exe"
                if (Test-Path $pyExe) {
                    $found += [pscustomobject]@{ Version = "$maj.$min"; Maj = $maj; Min = $min; Exe = $pyExe }
                }
            }
        }
    }
    if ($found.Count -gt 0) {
        $best = $found | Sort-Object -Property Maj, Min -Descending | Select-Object -First 1
        return @{ Exe = $best.Exe; PreArgs = @(); Version = $best.Version }
    }
    # Tier 3: well-known install paths as a last-ditch probe.
    foreach ($ver in @('312','311','310')) {
        foreach ($p in @(
            "$env:LocalAppData\Programs\Python\Python$ver\python.exe",
            "${env:ProgramFiles}\Python$ver\python.exe"
        )) {
            if (Test-Path $p) {
                $hit = _ProbeVersion $p @()
                if ($hit) { return $hit }
            }
        }
    }
    return $null
}

if ($PythonExe) {
    if (-not (Test-Path $PythonExe)) { throw "PythonExe path does not exist: $PythonExe" }
    $verRaw = & $PythonExe --version 2>&1
    if ($verRaw -notmatch 'Python\s+(\d+)\.(\d+)') { throw "PythonExe is not a Python executable: $PythonExe" }
    $py = @{ Exe = $PythonExe; PreArgs = @(); Version = "$($Matches[1]).$($Matches[2])" }
} else {
    $py = Find-Python
}
if (-not $py) {
    Write-Host "==> Python 3.10+ not found; auto-installing via winget"
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw @"
Python 3.10+ was not found and winget is not available on this machine.
Install Python 3.12 manually from https://www.python.org/downloads/windows/
(check 'Add Python to PATH' during install), then re-run this script.
"@
    }
    & winget install Python.Python.3.12 -e --silent --accept-source-agreements --accept-package-agreements
    # winget returns 0 on fresh install, 0x8A150061 (-1978335135) when already installed and up to date.
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335135) {
        throw "winget install Python failed (exit $LASTEXITCODE). Install Python 3.12 manually from https://www.python.org/downloads/windows/"
    }
    # winget --silent doesn't refresh PATH in the current session — pull the
    # updated machine + user PATH from the registry so we can find python.exe
    # without forcing the operator to open a new shell.
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
    $py = Find-Python
    if (-not $py) {
        throw @"
winget reported success but Python is still not findable.
Open a NEW elevated PowerShell window (so PATH refreshes) and re-run the install command,
or pass -PythonExe with the full path to python.exe.
"@
    }
}
Write-Host "==> using Python $($py.Version): $($py.Exe) $($py.PreArgs -join ' ')"

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
    # Splat PreArgs (empty array splats to nothing — no array-slice footgun).
    & $py.Exe @($py.PreArgs) -m venv $venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython)) {
        throw "venv creation failed (exit $LASTEXITCODE). The Python at $($py.Exe) may be missing the venv module."
    }
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
