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

  2. EVERYTHING TRANSIENT LIVES IN A TEMP WORKSPACE -- the source, the Python
     environment and every dependency sit under $env:TEMP and are deleted at
     the end. No PATH, registry or service setting is touched.

     NOTHING NEEDS TO BE INSTALLED FIRST. Neither Python nor git is required:
     without Python the official embeddable runtime is fetched into the
     workspace and thrown away with it, and without git the source comes down
     as a zip. This is deliberate rather than convenient -- the shipping
     product is an MSI that bundles that same embeddable runtime, because
     nobody puts Python on the machines their staff use every day, and a
     try-out that demanded one would be testing something the product isn't.

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

.PARAMETER JsonOut
  Write the probe results as JSON to this path. Opt-in on purpose: a file the
  operator did not ask for is a trace, and the inventory diff cannot see files.
  Everything in it is printed to the console regardless.

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
  Requires nothing preinstalled. Python 3.9+ and git are both used if present
  and both fall back to a download into the temp workspace if not -- asking
  someone to install two tools in order to try a third is a poor trade, and on
  a workstation installing Python is not on the table at all.
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
    [string]   $JsonOut,
    # Same runtime the MSI bundles. Overridable for an air-gapped mirror, which
    # is exactly why central/msi_builder.py exposes agent.python_embed_url.
    [string]   $PythonEmbedUrl = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip",
    [string]   $GetPipUrl      = "https://bootstrap.pypa.io/get-pip.py",
    [switch]   $SkipProbe,
    [switch]   $SkipSpooler,
    [switch]   $ForceZip,
    [switch]   $ForceEmbeddedPython,
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
    # Names only -- enough to prove nothing was added or removed. The
    # SilentlyContinue here is safe only because preflight already refused to run
    # on a machine lacking these cmdlets; otherwise an empty snapshot would be
    # indistinguishable from an empty spooler, and cleanup keys off it.
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
    #
    # It fails CLOSED. Get-ChildItem on a missing directory returns nothing
    # quietly, so "zero files scanned" and "zero offenders found" produce an
    # identical green line. On the one operation that cannot be undone, a guard
    # that passes because it inspected nothing is worse than no guard, because
    # it reads as reassurance.
    $files = @(Get-ChildItem -Path $TestDir -Filter *.py -Recurse -ErrorAction SilentlyContinue)
    if ($files.Count -eq 0) {
        throw "driver-staging guard found no test files under '$TestDir' -- refusing to run rather than trust a check that inspected nothing"
    }

    $offenders = @()
    foreach ($file in $files) {
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

        # $ErrorActionPreference='Stop' is set script-wide, and on Windows
        # PowerShell 5.1 that turns `2>&1` on a NATIVE command into a
        # script-terminating error: each stderr line becomes an ErrorRecord with
        # FullyQualifiedErrorId NativeCommandError, and 'Stop' promotes it.
        # Without relaxing it here, a perfectly good interpreter that writes
        # anything at all to stderr is thrown out and misreported as a
        # WindowsApps stub. Scoped to the call and restored immediately.
        $output = ''
        $exit = 1
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $output = (& $cmd.Source --version 2>&1 | Out-String)
            $exit = $LASTEXITCODE
        } catch {
            $output = ''
            $exit = 1
        } finally {
            $ErrorActionPreference = $prevEap
        }
        if ($exit -ne 0) { continue }
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

function Invoke-Download {
    param([string] $Uri, [string] $OutFile)

    # Some Windows builds still default to TLS 1.0/1.1, which python.org and
    # github.com both refuse. Restored afterwards rather than left set: when this
    # script is dot-sourced the change would otherwise outlive the run and
    # re-pin the caller's TLS settings -- a trace, and this script promises none.
    $prevProtocol = $null
    try {
        $prevProtocol = [Net.ServicePointManager]::SecurityProtocol
        [Net.ServicePointManager]::SecurityProtocol =
            $prevProtocol -bor [Net.SecurityProtocolType]::Tls12
    } catch {
        # Older .NET without the Tls12 member -- nothing to do but continue.
    }

    try {
        # -UseBasicParsing: on Windows PowerShell 5.1 the default path leans on
        # Internet Explorer's DOM engine, which fails outright on a machine where
        # IE has never been launched. Harmless no-op on 7.x.
        Invoke-WebRequest -Uri $Uri -OutFile $OutFile -UseBasicParsing
    } finally {
        if ($null -ne $prevProtocol) {
            try { [Net.ServicePointManager]::SecurityProtocol = $prevProtocol } catch { }
        }
    }
    if (-not (Test-Path $OutFile)) { throw "download of $Uri produced no file" }
}

function Get-EmbeddedPython {
    param([string] $Workspace, [string] $EmbedUrl, [string] $GetPipUrl)

    # WHY THIS EXISTS
    # Requiring an operator to install Python before they can try a printer tool
    # is backwards, and on a workstation it is unacceptable outright -- nobody
    # puts Python on the machines their staff use every day. The product answer
    # is the MSI, which bundles the official embeddable runtime (see
    # central/msi_builder.py). This mirrors that exact approach for the harness,
    # so the try-out has the same "installs nothing" property the real client has.
    #
    # It is also MORE removable than the system-Python path, not less: the whole
    # interpreter lives inside the temp workspace and is deleted with it. No
    # installer runs, no PATH entry is made, no registry key is written.
    $pyDir = Join-Path $Workspace "python-embed"
    $zip   = Join-Path $Workspace "python-embed.zip"

    Write-Host "  no system Python; fetching the embeddable runtime instead"
    Write-Host "  $EmbedUrl"
    Invoke-Download -Uri $EmbedUrl -OutFile $zip

    New-Item -ItemType Directory -Path $pyDir -Force | Out-Null
    Expand-Archive -Path $zip -DestinationPath $pyDir -Force

    $exe = Join-Path $pyDir "python.exe"
    if (-not (Test-Path $exe)) { throw "embeddable archive contained no python.exe" }

    # The embeddable distribution ships with `import site` commented out and
    # site-packages off the path, so anything pip installs stays invisible.
    # Same patch central/msi_builder.py applies for the same reason.
    $pths = @(Get-ChildItem -Path $pyDir -Filter "python*._pth")
    if ($pths.Count -eq 0) { throw "embeddable runtime has no python*._pth to patch" }
    foreach ($pth in $pths) {
        $kept = @()
        foreach ($line in (Get-Content -Path $pth.FullName)) {
            $t = $line.Trim()
            if ($t -eq 'import site' -or $t -eq '#import site' -or $t -eq '# import site') { continue }
            if ($t -eq 'Lib\site-packages' -or $t -eq 'Lib/site-packages') { continue }
            $kept += $line
        }
        $kept += 'Lib\site-packages'
        $kept += 'import site'
        Set-Content -Path $pth.FullName -Value $kept
    }

    # The embeddable runtime deliberately omits pip; get-pip.py is the official
    # way back in.
    $getPip = Join-Path $Workspace "get-pip.py"
    Write-Host "  bootstrapping pip"
    Invoke-Download -Uri $GetPipUrl -OutFile $getPip

    $exit = 1
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $exe $getPip --no-warn-script-location --quiet
        $exit = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevEap
    }
    if ($exit -ne 0) { throw "get-pip.py failed with exit code $exit" }

    return $exe
}

function Get-RepoSource {
    param([string] $RepoUrl, [string] $Ref, [string] $Workspace, [switch] $ForceZip)

    # git first when it's genuinely present: a shallow clone is fast and pins an
    # exact ref. But git is NOT on a stock Windows box, and requiring someone to
    # install one tool in order to try another is a bad trade -- so its absence
    # is a fallback, not an error.
    # $Ref lands in a URL below and in a --branch argument here. Constrain it to
    # ref-legal characters so a '..' segment cannot silently retarget the
    # download at a different repository path.
    if ($Ref -notmatch '^[A-Za-z0-9][A-Za-z0-9._/-]*$' -or $Ref -match '\.\.') {
        throw "refusing to fetch ref '$Ref': expected a branch name, tag or commit SHA"
    }

    $git = if ($ForceZip) { $null } else { Get-Command git -ErrorAction SilentlyContinue }
    if ($git) {
        $dest = Join-Path $Workspace "printer-nanny"
        Write-Host "  cloning $RepoUrl ($Ref) ..."

        # NOT `2>&1 | Out-Null`. On Windows PowerShell 5.1, merging a native
        # command's stderr into the success stream under
        # $ErrorActionPreference='Stop' makes every stderr line a
        # script-terminating NativeCommandError -- and git writes to stderr on
        # every failure path, so the fallback this function exists to provide
        # would never be reached. The whole point is to survive git failing.
        $cloneExit = 1
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & git clone --quiet --depth 1 --branch $Ref $RepoUrl $dest
            $cloneExit = $LASTEXITCODE
        } catch {
            $cloneExit = 1
        } finally {
            $ErrorActionPreference = $prevEap
        }

        if ($cloneExit -eq 0) { return $dest }
        Write-Host "  git clone failed (exit $cloneExit); falling back to the zip archive" -ForegroundColor Yellow
        Remove-Item -Recurse -Force $dest -ErrorAction SilentlyContinue
    } else {
        Write-Host "  git not present; fetching the source as a zip instead"
    }

    $base   = $RepoUrl -replace '\.git$', ''
    $zipUrl = "$base/archive/$Ref.zip"
    $zip    = Join-Path $Workspace "source.zip"
    $stage  = Join-Path $Workspace "unpacked"

    Write-Host "  downloading $zipUrl ..."
    Invoke-Download -Uri $zipUrl -OutFile $zip

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

$python = if ($ForceEmbeddedPython) { $null } else { Resolve-Python }
if ($python) {
    Write-Host "Python        : $($python.Path)  (v$($python.Version))"
} else {
    # NOT an error. Requiring someone to install Python before they can try a
    # printer tool is backwards -- and on a workstation it is exactly what the
    # product must never do, since nobody puts Python on the machines their
    # staff use every day. The embeddable runtime is fetched into the temp
    # workspace instead, which is what the MSI does and what this run will do
    # a few lines further down.
    #
    # Worth noting what was almost certainly found and rejected: a python.exe
    # under WindowsApps is an app execution alias, a shim that answers PATH
    # lookups whether or not any interpreter is installed behind it.
    Write-Host "Python        : none installed; will use a private embeddable runtime"
}

# The whole removability proof rests on being able to enumerate printers, ports
# and drivers. `-ErrorAction SilentlyContinue` on those calls cannot distinguish
# "nothing installed" from "cmdlet unavailable", and an empty BEFORE snapshot is
# actively dangerous: cleanup removes PNTest-* queues that are absent from it, so
# a silently-empty snapshot would license deleting a PNTest-* queue the operator
# owns. Refuse up front rather than proceed with a proof we cannot make.
foreach ($required in @('Get-Printer', 'Get-PrinterPort', 'Get-PrinterDriver')) {
    if (-not (Get-Command $required -ErrorAction SilentlyContinue)) {
        Write-Host ""
        Write-Host "$required is unavailable on this system." -ForegroundColor Red
        Write-Host "Without it the before/after inventory cannot be taken, and this" -ForegroundColor Red
        Write-Host "script will not touch a machine whose state it cannot verify it" -ForegroundColor Red
        Write-Host "restored. (The PrintManagement module ships with Windows 8/2012+.)" -ForegroundColor Red
        exit 1
    }
}

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
        $repo = Get-RepoSource -RepoUrl $RepoUrl -Ref $Ref -Workspace $script:Workspace -ForceZip:$ForceZip
    }

    Write-Section "Driver-staging guard"
    Assert-NoDriverStaging -TestDir (Join-Path $repo "tests\windows")

    Write-Section "Python environment (private to this run)"
    if ($python) {
        # System Python present: a venv keeps our packages out of it.
        $venv = Join-Path $script:Workspace "venv"
        & $python.Path -m venv $venv
        if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
        $venvPy = Join-Path $venv "Scripts\python.exe"
        Write-Host "  venv: $venvPy"
    } else {
        # No system Python: the embeddable tree IS the isolation. It needs no
        # venv (the embeddable distribution does not support one cleanly), and
        # because it lives inside the workspace it is removed with everything
        # else -- nothing is installed on the machine at any point.
        $venvPy = Get-EmbeddedPython -Workspace $script:Workspace -EmbedUrl $PythonEmbedUrl -GetPipUrl $GetPipUrl
        Write-Host "  embedded: $venvPy"
    }

    Write-Host "  installing (this takes a minute) ..."

    # Native command under $ErrorActionPreference='Stop': pip writes progress and
    # warnings to stderr, and on Windows PowerShell 5.1 a merged stderr line
    # would terminate the script. Nothing is merged here, but the preference is
    # relaxed around the calls so a chatty pip cannot end the run either.
    $pipExit = 1
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    Push-Location $repo
    try {
        & $venvPy -m pip install --upgrade pip --quiet
        & $venvPy -m pip install -e ".[dev,agent,agent-mdns]" --quiet
        $pipExit = $LASTEXITCODE
    } finally {
        Pop-Location
        $ErrorActionPreference = $prevEap
    }
    if ($pipExit -ne 0) { throw "pip install failed with exit code $pipExit" }
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

        # Deliberately NOT copied to the Desktop. An earlier version did, which
        # quietly broke the script's central promise: the file persisted after
        # the run, the inventory diff cannot see it (it only compares printers,
        # ports and drivers), and the script then announced that nothing had
        # been left behind. A file the operator did not ask for is a trace.
        #
        # So the JSON is written only where they name it. Everything it contains
        # was already printed to the console above.
        if ($JsonOut) {
            $probeJson = Join-Path $script:Workspace "probe.json"
            if (Test-Path $probeJson) {
                Copy-Item $probeJson $JsonOut -Force
                Write-Host "`n  probe JSON written to $JsonOut (you asked for it; nothing else persists)" -ForegroundColor Green
            }
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
                # A surviving workspace is a leak, and the inventory diff below
                # cannot see it -- that diff only compares printers, ports and
                # drivers. Reported loudly and failed, rather than mentioned in
                # passing and then contradicted by "nothing left behind".
                Write-Host "  LEFT BEHIND: $($script:Workspace)" -ForegroundColor Red
                Write-Host "  (delete it by hand: Remove-Item -Recurse -Force '$($script:Workspace)')" -ForegroundColor Red
                $script:ExitCode = 1
            } else {
                Write-Host "  workspace removed"
            }
        }
    }

    Write-Section "Inventory AFTER -- proof of removal"
    $after = Get-Inventory
    $clean = Compare-Inventory -Before $script:Before -After $after

    Write-Host ""
    if (-not $clean) {
        Write-Host "  DIFFERENCES FOUND -- see above. Please report this." -ForegroundColor Red
        $script:ExitCode = 1
    } elseif ($script:ExitCode -ne 0 -and $script:Workspace -and (Test-Path $script:Workspace)) {
        # The inventory is clean but a file-level leak was reported above. Saying
        # "nothing left behind" here would contradict it, so it doesn't.
        Write-Host "  Printer state restored, but the workspace above survived." -ForegroundColor Yellow
    } else {
        Write-Host "  Machine is exactly as it was found. Nothing left behind." -ForegroundColor Green
    }
}

exit $script:ExitCode
