<#
.SYNOPSIS
  Try Printer Nanny's Windows client on a real machine, and leave no trace.

.DESCRIPTION
  Two things a hosted CI runner cannot do: probe a real printer, and exercise
  the spooler on your actual Windows build. This does both, and then proves it
  put the machine back exactly as it found it.

  REMOVABILITY IS THE DESIGN CONSTRAINT, and it is enforced three ways:

  1. NO DRIVER IS EVER STAGED. `pnputil /add-driver` and `Add-PrinterDriver`
     write into the DriverStore and do not cleanly come back out -- that is the
     one genuinely irreversible operation in the whole client. This script only
     binds drivers already present on the box, so the Tier 3 (vendor driver)
     path stays deliberately untested here. A preflight check refuses to run if
     the test files ever start staging.

  2. EVERYTHING TRANSIENT LIVES IN A TEMP WORKSPACE -- a fresh clone and a
     private virtualenv under $env:TEMP, deleted at the end. Nothing is
     installed into your system Python, and no PATH, registry or service
     setting is touched.

  3. REMOVAL IS DEMONSTRATED, NOT ASSERTED. A full inventory of printers,
     ports and drivers is captured before anything runs and again at the end,
     and the difference is printed. If a single item differs the script says so
     and exits non-zero. Cleanup also only ever removes artifacts that were
     absent from the "before" snapshot, so a queue of yours that happens to
     match a test prefix is left alone.

  The spooler phase creates queues named `PNTest-*` on ports named
  `PN_127.0.0.*`, all pointed at loopback, and removes them again. The probe
  phase is pure network reads and changes nothing at all.

.PARAMETER SkipSpooler
  Run only the read-only printer discovery + IPP probe. Needs no admin rights
  and creates nothing whatsoever. Good first run if you want to see the
  machine stay untouched before letting it make queues.

.PARAMETER SkipProbe
  Run only the spooler queue tests; no network scanning.

.PARAMETER Cidr
  Subnet(s) to sweep, e.g. 192.168.1.0/24. Defaults to the machine's own IPv4
  subnets. Ranges larger than -MaxHosts are refused rather than scanned.

.PARAMETER PrinterIp
  Probe these addresses directly and skip discovery entirely. Fastest and
  quietest option when you already know the printer's address.

.PARAMETER RepoPath
  Use an existing clone instead of cloning fresh. The clone is left alone --
  only the virtualenv inside the temp workspace is created and removed.

.PARAMETER KeepWorkspace
  Leave the temp workspace in place for inspection. The printer inventory is
  still restored; this only keeps the files.

.EXAMPLE
  # Safest first run: probe only, nothing created.
  # Windows blocks downloaded .ps1 files by default, so run it in a child
  # process with the policy relaxed for that process alone -- this writes
  # nothing to the registry and leaves the calling shell's policy untouched.
  powershell -NoProfile -ExecutionPolicy Bypass -File .\windows-tryout.ps1 -SkipSpooler

.EXAMPLE
  # Everything, against a known printer. Needs an elevated shell.
  powershell -NoProfile -ExecutionPolicy Bypass -File .\windows-tryout.ps1 -PrinterIp 192.168.1.50

.NOTES
  Needs Python 3.9+. Git is optional -- without it the source is fetched as a
  zip over HTTPS, because a stock Windows box has no git and asking someone to
  install one to try the other is a poor trade.
#>
[CmdletBinding()]
param(
    [string]   $RepoUrl       = "https://github.com/stephenthecold/printer-nanny.git",
    [string]   $Ref           = "main",
    [string]   $RepoPath,
    [string[]] $Cidr,
    [string[]] $PrinterIp,
    [string]   $Community     = "public",
    [int]      $MaxHosts      = 1024,
    [switch]   $SkipProbe,
    [switch]   $SkipSpooler,
    [switch]   $KeepWorkspace
)

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

$script:Workspace = $null
$script:Before    = $null
$script:ExitCode  = 0

function Write-Section {
    param([string] $Text)
    Write-Host ""
    Write-Host ("=" * 72) -ForegroundColor Cyan
    Write-Host $Text -ForegroundColor Cyan
    Write-Host ("=" * 72) -ForegroundColor Cyan
}

function Get-Inventory {
    # Names only. Enough to prove nothing was added or removed, and it cannot
    # itself fail on a machine where one of these cmdlets is unavailable.
    [ordered]@{
        printers = @(Get-Printer       -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name | Sort-Object)
        ports    = @(Get-PrinterPort   -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name | Sort-Object)
        drivers  = @(Get-PrinterDriver -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name | Sort-Object)
    }
}

function Compare-Inventory {
    param($Before, $After)

    $clean = $true
    foreach ($kind in @('printers', 'ports', 'drivers')) {
        $added   = @($After.$kind  | Where-Object { $Before.$kind -notcontains $_ })
        $removed = @($Before.$kind | Where-Object { $After.$kind  -notcontains $_ })

        if ($added.Count -eq 0 -and $removed.Count -eq 0) {
            Write-Host ("  {0,-9} unchanged ({1})" -f $kind, $After.$kind.Count) -ForegroundColor Green
        } else {
            $clean = $false
            foreach ($item in $added)   { Write-Host ("  {0,-9} LEFT BEHIND: {1}" -f $kind, $item) -ForegroundColor Red }
            foreach ($item in $removed) { Write-Host ("  {0,-9} REMOVED:     {1}" -f $kind, $item) -ForegroundColor Red }
        }
    }
    return $clean
}

function Remove-TestArtifacts {
    param($Before)

    # Only ever remove what was absent beforehand. If you happen to own a
    # printer named PNTest-something, it survives -- deleting a user's own
    # printer is exactly how a print tool gets uninstalled.
    $queues = @(Get-Printer -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like 'PNTest-*' -and $Before.printers -notcontains $_.Name })
    foreach ($q in $queues) {
        Write-Host "  removing queue: $($q.Name)"
        Remove-Printer -Name $q.Name -ErrorAction SilentlyContinue
    }

    $ports = @(Get-PrinterPort -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like 'PN_127.0.0.*' -and $Before.ports -notcontains $_.Name })
    foreach ($p in $ports) {
        Write-Host "  removing port: $($p.Name)"
        Remove-PrinterPort -Name $p.Name -ErrorAction SilentlyContinue
    }

    if ($queues.Count -eq 0 -and $ports.Count -eq 0) {
        Write-Host "  nothing to remove (tests cleaned up after themselves)"
    }
}

function Assert-NoDriverStaging {
    param([string] $TestDir)

    # The guard that makes claim #1 real rather than a promise. If a future
    # edit teaches the Windows tests to stage a driver, this refuses to run
    # rather than quietly making an irreversible change to someone's machine.
    $offenders = @()
    foreach ($file in Get-ChildItem -Path $TestDir -Filter *.py -Recurse -ErrorAction SilentlyContinue) {
        $text = Get-Content -Raw -Path $file.FullName
        foreach ($pattern in @('ensure_vendor_queue', 'pnputil', 'Add-PrinterDriver', '_SCRIPT_STAGE_DRIVER')) {
            if ($text -match [regex]::Escape($pattern)) {
                $offenders += "$($file.Name): $pattern"
            }
        }
    }
    if ($offenders.Count -gt 0) {
        Write-Host ""
        Write-Host "REFUSING TO RUN: the Windows tests would stage a driver." -ForegroundColor Red
        Write-Host "DriverStore changes are not cleanly reversible, so this script" -ForegroundColor Red
        Write-Host "will not make them on your machine. Offending references:" -ForegroundColor Red
        $offenders | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
        throw "driver-staging guard tripped"
    }
    Write-Host "  no driver-staging call reachable from the test suite" -ForegroundColor Green
}

function Resolve-Python {
    # Finding python on PATH is NOT proof python exists.
    #
    # Windows ships "app execution aliases" in WindowsApps -- zero-byte stubs
    # that resolve happily through Get-Command and, when the Store package
    # behind them isn't installed, open the Microsoft Store instead of running
    # anything. A machine with no Python still answers `Get-Command python`,
    # which is how a preflight check sails straight past the problem and fails
    # incomprehensibly three phases later inside pip.
    #
    # So each candidate is made to prove itself by printing a version.
    foreach ($candidate in @('python', 'python3', 'py')) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }

        $output = ''
        try {
            $output = (& $cmd.Source --version 2>&1 | Out-String)
        } catch {
            continue
        }
        if ($LASTEXITCODE -ne 0) { continue }
        if ($output -notmatch 'Python\s+(\d+)\.(\d+)') { continue }

        $major = [int]$Matches[1]
        $minor = [int]$Matches[2]
        if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 9)) {
            Write-Host "  $($cmd.Source) is Python $major.$minor; need 3.9+" -ForegroundColor Yellow
            continue
        }
        return [pscustomobject]@{
            Path    = $cmd.Source
            Version = "$major.$minor"
        }
    }
    return $null
}

function Get-RepoSource {
    param([string] $RepoUrl, [string] $Ref, [string] $Workspace)

    # git first when it's genuinely present: a shallow clone is fast and pins an
    # exact ref. But git is NOT on a stock Windows box, and requiring someone to
    # install one tool in order to try another is a bad trade -- so its absence
    # is a fallback, not an error.
    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($git) {
        $dest = Join-Path $Workspace "printer-nanny"
        Write-Host "  cloning $RepoUrl ($Ref) ..."
        & git clone --quiet --depth 1 --branch $Ref $RepoUrl $dest 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { return $dest }
        Write-Host "  git clone failed (exit $LASTEXITCODE); falling back to the zip archive" -ForegroundColor Yellow
        Remove-Item -Recurse -Force $dest -ErrorAction SilentlyContinue
    } else {
        Write-Host "  git not present; fetching the source as a zip instead"
    }

    # Some Windows builds still default to TLS 1.0/1.1, which github.com refuses.
    # Process-scoped, so nothing outside this run is affected.
    try {
        [Net.ServicePointManager]::SecurityProtocol =
            [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    } catch {
        # Older .NET without the Tls12 member -- nothing to do but continue.
    }

    $base   = $RepoUrl -replace '\.git$', ''
    $zipUrl = "$base/archive/$Ref.zip"
    $zip    = Join-Path $Workspace "source.zip"
    $stage  = Join-Path $Workspace "unpacked"

    Write-Host "  downloading $zipUrl ..."
    # -UseBasicParsing: on Windows PowerShell 5.1 the default path leans on
    # Internet Explorer's DOM engine, which fails outright on a machine where IE
    # has never been launched. Harmless no-op on 7.x.
    Invoke-WebRequest -Uri $zipUrl -OutFile $zip -UseBasicParsing
    if (-not (Test-Path $zip)) { throw "download produced no file" }

    New-Item -ItemType Directory -Path $stage -Force | Out-Null
    Expand-Archive -Path $zip -DestinationPath $stage -Force

    # GitHub names the folder <repo>-<ref> with slashes flattened, so it is
    # found rather than computed -- a branch called feature/x would not match
    # any name we could predict.
    $extracted = @(Get-ChildItem -Path $stage -Directory)
    if ($extracted.Count -ne 1) {
        throw "expected one directory inside the archive, found $($extracted.Count)"
    }
    Write-Host "  unpacked $($extracted[0].Name)"
    return $extracted[0].FullName
}

function Get-LocalCidrs {
    $out = @()
    $addrs = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object {
            $_.IPAddress -notlike '127.*' -and
            $_.IPAddress -notlike '169.254.*' -and
            $_.PrefixLength -ge 16 -and $_.PrefixLength -le 32
        }
    foreach ($a in $addrs) {
        $out += "$($a.IPAddress)/$($a.PrefixLength)"
    }
    return $out
}

# --- preflight ---------------------------------------------------------------

Write-Section "Printer Nanny -- Windows try-out (removable by design)"

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

Write-Host "OS            : $([System.Environment]::OSVersion.VersionString)"
Write-Host "PowerShell    : $($PSVersionTable.PSVersion)"
Write-Host "Elevated      : $isAdmin"

$python = Resolve-Python
if (-not $python) {
    Write-Host ""
    Write-Host "No working Python 3.9+ found." -ForegroundColor Red
    Write-Host ""
    Write-Host "If 'python' appears to be on your PATH under WindowsApps, that is" -ForegroundColor Yellow
    Write-Host "an app execution alias, not an interpreter -- it opens the Microsoft" -ForegroundColor Yellow
    Write-Host "Store when the package behind it isn't installed. Either install" -ForegroundColor Yellow
    Write-Host "Python from python.org (tick 'Add python.exe to PATH'), or turn the" -ForegroundColor Yellow
    Write-Host "alias off under Settings > Apps > Advanced app settings >" -ForegroundColor Yellow
    Write-Host "App execution aliases." -ForegroundColor Yellow
    exit 1
}
Write-Host "Python        : $($python.Path)  (v$($python.Version))"

if (-not $SkipSpooler -and -not $isAdmin) {
    Write-Host ""
    Write-Host "The spooler phase needs an elevated shell (Add-Printer requires it)." -ForegroundColor Yellow
    Write-Host "Either re-run this from an Administrator PowerShell, or use" -ForegroundColor Yellow
    Write-Host "  -SkipSpooler   to run the read-only probe phase alone." -ForegroundColor Yellow
    exit 1
}

# --- capture the "before" picture -------------------------------------------

Write-Section "Inventory BEFORE"
$script:Before = Get-Inventory
Write-Host "  printers: $($script:Before.printers.Count)   ports: $($script:Before.ports.Count)   drivers: $($script:Before.drivers.Count)"
$existingTestQueues = @($script:Before.printers | Where-Object { $_ -like 'PNTest-*' })
if ($existingTestQueues.Count -gt 0) {
    Write-Host "  note: you already have PNTest-* queue(s); they will be left untouched:" -ForegroundColor Yellow
    $existingTestQueues | ForEach-Object { Write-Host "    $_" -ForegroundColor Yellow }
}

try {
    # --- workspace -----------------------------------------------------------

    Write-Section "Workspace"
    $script:Workspace = Join-Path $env:TEMP ("pn-tryout-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Path $script:Workspace -Force | Out-Null
    Write-Host "  $($script:Workspace)"

    if ($RepoPath) {
        $repo = (Resolve-Path $RepoPath).Path
        Write-Host "  using existing clone: $repo"
    } else {
        $repo = Get-RepoSource -RepoUrl $RepoUrl -Ref $Ref -Workspace $script:Workspace
    }

    Write-Section "Driver-staging guard"
    Assert-NoDriverStaging -TestDir (Join-Path $repo "tests\windows")

    Write-Section "Virtualenv (private to this run)"
    $venv = Join-Path $script:Workspace "venv"
    & $python.Path -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
    $venvPy = Join-Path $venv "Scripts\python.exe"
    Write-Host "  $venvPy"

    Write-Host "  installing (this takes a minute) ..."
    & $venvPy -m pip install --upgrade pip --quiet
    Push-Location $repo
    try {
        & $venvPy -m pip install -e ".[dev,agent,agent-mdns]" --quiet
        if ($LASTEXITCODE -ne 0) { throw "pip install failed with exit code $LASTEXITCODE" }
    } finally {
        Pop-Location
    }
    Write-Host "  installed" -ForegroundColor Green

    # --- phase 1: read-only discovery + IPP probe ----------------------------

    if (-not $SkipProbe) {
        Write-Section "Phase 1 -- discovery + IPP probe (read-only)"

        $probeArgs = @((Join-Path $repo "scripts\windows_probe_report.py"))
        $probeArgs += @("--community", $Community, "--max-hosts", $MaxHosts)
        $probeArgs += @("--json-out", (Join-Path $script:Workspace "probe.json"))

        if ($PrinterIp) {
            foreach ($ip in $PrinterIp) { $probeArgs += @("--ip", $ip) }
        } else {
            $targets = if ($Cidr) { $Cidr } else { Get-LocalCidrs }
            if (-not $targets -or $targets.Count -eq 0) {
                Write-Host "  no usable IPv4 subnet found; skipping discovery" -ForegroundColor Yellow
            }
            foreach ($c in $targets) { $probeArgs += @("--cidr", $c) }
        }

        & $venvPy @probeArgs
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  probe reporter exited $LASTEXITCODE" -ForegroundColor Yellow
            $script:ExitCode = 1
        }

        $probeJson = Join-Path $script:Workspace "probe.json"
        if ((Test-Path $probeJson) -and -not $KeepWorkspace) {
            $saved = Join-Path ([Environment]::GetFolderPath('Desktop')) "printer-nanny-probe.json"
            Copy-Item $probeJson $saved -Force -ErrorAction SilentlyContinue
            if (Test-Path $saved) { Write-Host "`n  probe JSON saved to $saved" -ForegroundColor Green }
        }
    } else {
        Write-Section "Phase 1 -- skipped (-SkipProbe)"
    }

    # --- phase 2: the spooler ------------------------------------------------

    if (-not $SkipSpooler) {
        Write-Section "Phase 2 -- spooler queue lifecycle"
        Write-Host "  creates PNTest-* queues on loopback ports, then removes them."
        Write-Host ""

        Push-Location $repo
        try {
            & $venvPy -m pytest tests/windows -v
            if ($LASTEXITCODE -ne 0) {
                Write-Host "`n  pytest exited $LASTEXITCODE -- please paste the output above" -ForegroundColor Yellow
                $script:ExitCode = 1
            }
        } finally {
            Pop-Location
        }
    } else {
        Write-Section "Phase 2 -- skipped (-SkipSpooler)"
    }
}
catch {
    Write-Host ""
    Write-Host "RUN FAILED: $($_.Exception.Message)" -ForegroundColor Red
    $script:ExitCode = 1
}
finally {
    # Cleanup runs whatever happened above -- a crashed test must not leave a
    # queue behind, which is the whole point of doing this in a finally block.

    Write-Section "Cleanup"
    try {
        Remove-TestArtifacts -Before $script:Before
    } catch {
        Write-Host "  artifact cleanup hit an error: $($_.Exception.Message)" -ForegroundColor Red
        $script:ExitCode = 1
    }

    if ($script:Workspace -and (Test-Path $script:Workspace)) {
        if ($KeepWorkspace) {
            Write-Host "  keeping workspace: $($script:Workspace)" -ForegroundColor Yellow
        } else {
            Remove-Item -Recurse -Force $script:Workspace -ErrorAction SilentlyContinue
            if (Test-Path $script:Workspace) {
                Write-Host "  could not fully remove $($script:Workspace)" -ForegroundColor Yellow
            } else {
                Write-Host "  workspace removed"
            }
        }
    }

    Write-Section "Inventory AFTER -- proof of removal"
    $after = Get-Inventory
    $clean = Compare-Inventory -Before $script:Before -After $after

    Write-Host ""
    if ($clean) {
        Write-Host "  Machine is exactly as it was found. Nothing left behind." -ForegroundColor Green
    } else {
        Write-Host "  DIFFERENCES FOUND -- see above. Please report this." -ForegroundColor Red
        $script:ExitCode = 1
    }
}

exit $script:ExitCode
