"""Build a self-contained Windows ``.msi`` installer for the site agent, in-container.

The MSP architecture promise is "agents reach only central". This module lets an
operator click a button in the dashboard and get a ready-to-run MSI for an
*enrolled* agent -- no Python, no winget, no internet on the target server. The
MSI bundles:

  * the official **Python embeddable runtime** (a standalone python.exe; no
    install step on the target),
  * the **agent package + its dependencies** (pip-installed into the runtime's
    ``Lib\\site-packages``),
  * **NSSM** (the service wrapper, mirrored through central like the .ps1 path),
  * a generated **config.toml** with the agent's enrollment baked in
    (central_url / agent_id / api_key),

and registers a Windows service (``PrinterNannyAgent``) entirely declaratively --
no install-time custom actions -- so it works the same on Server 2016 through
2025. The service runs ``python -m printer_nanny_agent --config <toml> run`` via
NSSM; NSSM's own parameters are written through the MSI Registry table.

The build runs inside the central container using **msitools** (``wixl`` compiles
a WiX source to a real MSI; ``msiinfo`` / ``msiextract`` validate it). Heavy
artifacts (the embeddable zip, NSSM, and the pip-installed runtime tree) are
cached under ``PN_CACHE_DIR`` so only the first build pays the download/pip cost;
per-agent builds after that just drop in a fresh config.toml and re-link.

This module does NOT import FastAPI -- it's a plain library so it can be unit
tested and called from a CLI as well as the dashboard route.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape, quoteattr

import httpx

log = logging.getLogger("printer_nanny.msi")

# --------------------------------------------------------------------------- #
# Stable identity for the product line. The UpgradeCode must NEVER change -- it
# is what lets a newer MSI upgrade an older install in place rather than
# installing a second copy side by side. Generated once; hard-coded forever.
# --------------------------------------------------------------------------- #
UPGRADE_CODE = "7B0E2C4A-3F1D-4A8E-9C2B-5D6E7F801234"
SERVICE_NAME = "PrinterNannyAgent"
SERVICE_DISPLAY = "Printer Nanny Agent"
SERVICE_DESC = "Printer Nanny site agent -- SNMP collector that reports to the central server."
PRODUCT_NAME = "Printer Nanny Agent"
MANUFACTURER = "Printer Nanny"

# Default Python embeddable runtime. Overridable (air-gapped mirrors) via the
# ``agent.python_embed_url`` setting / ``PN_PYTHON_EMBED_URL`` env. The version
# string is parsed out of the URL filename so the cache key stays truthful even
# when the operator pins a different build.
DEFAULT_PYTHON_EMBED_VERSION = "3.12.10"
DEFAULT_PYTHON_EMBED_URL = (
    f"https://www.python.org/ftp/python/{DEFAULT_PYTHON_EMBED_VERSION}/"
    f"python-{DEFAULT_PYTHON_EMBED_VERSION}-embed-amd64.zip"
)

# Deterministic namespace for component GUIDs derived from install paths, so a
# rebuilt MSI of the same version yields the same component identity (correct
# Windows Installer component rules) instead of a random GUID each time.
_GUID_NS = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


@dataclass
class MsiCapability:
    """Whether this central image can build MSIs, and why not if it can't."""

    available: bool
    reason: str
    wixl: Optional[str] = None
    msiinfo: Optional[str] = None


def msi_build_available() -> MsiCapability:
    """Probe for the msitools toolchain (``wixl`` + ``msiinfo``).

    Returns a capability object the dashboard uses to decide whether to offer
    the download or show a "rebuild your central image" hint -- never a 500.
    """
    wixl = shutil.which("wixl")
    msiinfo = shutil.which("msiinfo")
    if not wixl or not msiinfo:
        missing = ", ".join(
            name for name, path in (("wixl", wixl), ("msiinfo", msiinfo)) if not path
        )
        return MsiCapability(
            available=False,
            reason=(
                f"MSI build tools not installed in this central image (missing: {missing}). "
                "Rebuild central from deploy/Dockerfile, which apt-installs msitools + wixl."
            ),
            wixl=wixl,
            msiinfo=msiinfo,
        )
    return MsiCapability(available=True, reason="ok", wixl=wixl, msiinfo=msiinfo)


# --------------------------------------------------------------------------- #
# Cache / artifact helpers
# --------------------------------------------------------------------------- #
def _cache_dir() -> Path:
    return Path(os.environ.get("PN_CACHE_DIR", "/var/lib/printer-nanny/cache"))


def _embed_version_from_url(url: str) -> str:
    """Pull ``X.Y.Z`` out of a python-X.Y.Z-embed-amd64.zip URL (best effort)."""
    name = url.rsplit("/", 1)[-1]
    # python-3.12.10-embed-amd64.zip -> 3.12.10
    parts = name.split("-")
    for part in parts:
        bits = part.split(".")
        if len(bits) >= 2 and all(b.isdigit() for b in bits):
            return part
    return DEFAULT_PYTHON_EMBED_VERSION


def _ensure_python_embed(url: str, cache: Path) -> Path:
    """Resolve the Python embeddable zip, downloading + caching if needed.

    Air-gapped deployments can point ``agent.python_embed_url`` at a local path
    or ``file://`` URL (or just drop the zip in the cache dir under its expected
    name) so the build never reaches out to python.org.
    """
    version = _embed_version_from_url(url)
    # Local path / file:// -> use the operator-staged zip directly.
    local = url[len("file://"):] if url.startswith("file://") else url
    if "://" not in url or url.startswith("file://"):
        p = Path(local)
        if p.is_file():
            return p
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / f"python-{version}-embed-amd64.zip"
    if dest.exists() and dest.stat().st_size > 1_000_000:
        return dest
    log.info("downloading Python embeddable runtime from %s", url)
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
    if len(resp.content) < 1_000_000:
        raise RuntimeError(
            f"Python embeddable download from {url} was suspiciously small "
            f"({len(resp.content)} bytes) -- likely an HTML error page."
        )
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(resp.content)
    tmp.replace(dest)
    return dest


def _ensure_nssm(cache: Path) -> Path:
    """Reuse the dashboard installer's NSSM mirror (so we cache it once, shared)."""
    # Imported lazily to avoid a central.dashboard import at module load.
    from central.dashboard.installer import _nssm_cache_path, _populate_nssm_cache

    nssm = _nssm_cache_path("x64")
    if not nssm.exists():
        _populate_nssm_cache()
    return nssm


# --------------------------------------------------------------------------- #
# Runtime tree staging (cached per agent-version + embed-version)
# --------------------------------------------------------------------------- #
def _patch_pth(python_dir: Path) -> None:
    """Enable ``Lib\\site-packages`` + ``import site`` in the embeddable ._pth.

    The embeddable distribution ships with site disabled and site-packages off
    the path, so installed packages are invisible until we opt back in. We
    rewrite the ``pythonNNN._pth`` to add the site-packages dir and re-enable
    site so ``python -m printer_nanny_agent`` can import the agent + its deps.
    """
    pths = list(python_dir.glob("python*._pth"))
    if not pths:
        raise RuntimeError("embeddable runtime has no python*._pth to patch")
    for pth in pths:  # normally exactly one; patch all defensively
        lines = pth.read_text().splitlines()
        out: list[str] = []
        have_site_pkgs = False
        for line in lines:
            stripped = line.strip()
            if stripped in ("import site", "#import site", "# import site"):
                # normalize to enabled
                continue
            if stripped in ("Lib\\site-packages", "Lib/site-packages"):
                have_site_pkgs = True
            out.append(line)
        if not have_site_pkgs:
            out.append("Lib\\site-packages")
        out.append("import site")
        pth.write_text("\n".join(out) + "\n")


def _pip_install_agent(site_packages: Path, agent_src: Path, python_exe: str) -> None:
    """pip-install the agent + deps into the runtime's site-packages.

    The agent and all its runtime deps (httpx, pysnmp, pyasn1, ...) are
    pure-Python, so installing them on the Linux build host with ``--target``
    produces a tree that runs unchanged on Windows. ``--no-compile`` keeps the
    payload free of host-specific .pyc files.
    """
    site_packages.mkdir(parents=True, exist_ok=True)
    cmd = [
        python_exe, "-m", "pip", "install",
        "--no-compile",
        "--target", str(site_packages),
        str(agent_src),
    ]
    log.info("pip installing agent into runtime: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "pip install of the agent into the MSI runtime failed:\n"
            + proc.stdout[-2000:] + "\n" + proc.stderr[-2000:]
        )


def _runtime_cache_dir(cache: Path, agent_version: str, embed_version: str) -> Path:
    return cache / "msi" / f"rt-{agent_version}-py{embed_version}"


def _ensure_runtime_tree(
    *, cache: Path, embed_url: str, agent_src: Path, agent_version: str,
    python_exe: str,
) -> Path:
    """Stage (and cache) the version-pinned runtime: extracted embeddable Python
    with the agent + deps installed, plus nssm.exe. Returns the runtime dir.

    Cache key is (agent base version, embeddable version): the expensive
    download + pip step happens once; every per-agent build then just copies
    this tree and drops in a fresh config.toml.
    """
    embed_version = _embed_version_from_url(embed_url)
    rt = _runtime_cache_dir(cache, agent_version, embed_version)
    if (rt / "python" / "python.exe").exists() and (rt / "nssm.exe").exists():
        return rt

    embed_zip = _ensure_python_embed(embed_url, cache)
    nssm = _ensure_nssm(cache)

    # Stage into a UNIQUE dir per build (never a shared ``.building`` path): two
    # concurrent cold builds must not delete each other's in-progress tree or
    # publish a half-built one. We then publish atomically with a single
    # rename; whoever renames first wins and the loser yields to that result.
    rt.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=str(rt.parent), prefix=rt.name + ".building-"))
    try:
        python_dir = staging / "python"
        python_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(embed_zip) as zf:
            zf.extractall(python_dir)
        _patch_pth(python_dir)
        _pip_install_agent(python_dir / "Lib" / "site-packages", agent_src, python_exe)
        shutil.copy2(nssm, staging / "nssm.exe")

        # Another build may have published the same version while we worked.
        if (rt / "python" / "python.exe").exists():
            return rt
        try:
            # rename(2) into a missing/empty target is atomic; into a non-empty
            # dir it fails (ENOTEMPTY) -- which means a concurrent build won.
            staging.replace(rt)
            staging = None  # ownership transferred; don't clean it below
        except OSError:
            if (rt / "python" / "python.exe").exists():
                return rt
            raise
        return rt
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Config + WiX source generation
# --------------------------------------------------------------------------- #
def _toml_escape(s: str) -> str:
    """Escape a value for a TOML basic (double-quoted) string.

    The API key comes from a form field, so an unescaped ``"`` or ``\\`` would
    emit invalid TOML and a silently un-startable agent. Escape the characters
    TOML requires (backslash, quote, and the control chars that have escapes).
    """
    return (
        s.replace("\\", "\\\\").replace('"', '\\"')
        .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    )


def render_config_toml(
    *, central_url: str, agent_id: int, api_key: str, verify_tls: bool = True
) -> str:
    """Render the agent.toml baked into the MSI. Mirrors install-agent.ps1."""
    return (
        "# Generated by the Printer Nanny MSI builder -- operational settings "
        "(subnets, SNMP, intervals)\n"
        "# are managed in the central UI, not here.\n"
        f'central_url = "{_toml_escape(central_url.rstrip("/"))}"\n'
        f"agent_id = {int(agent_id)}\n"
        f'api_key = "{_toml_escape(api_key)}"\n'
        f"verify_tls = {'true' if verify_tls else 'false'}\n"
    )


def _ident(prefix: str, n: int) -> str:
    return f"{prefix}{n}"


def _guid_for(relpath: str) -> str:
    return str(uuid.uuid5(_GUID_NS, relpath)).upper()


def generate_wxs(payload_dir: Path, *, product_version: str) -> str:
    """Generate the WiX source for ``payload_dir`` as a perMachine x64 MSI.

    ``payload_dir`` is the install tree exactly as it should land under the
    install directory (``python/``, ``nssm.exe``, ``config.toml``). We emit one
    Component per directory-that-contains-files (keypath = first file) to keep
    the component count sane, attach the service + NSSM registry parameters to
    the root component, and reference every component from a single Feature.

    Returns the .wxs XML as a string. File @Source paths are RELATIVE to
    ``payload_dir`` -- invoke wixl with that as its working directory.
    """
    payload_dir = payload_dir.resolve()
    comp_refs: list[str] = []
    components_xml: list[str] = []

    dir_counter = [0]
    comp_counter = [0]
    file_counter = [0]

    def next_dir() -> str:
        dir_counter[0] += 1
        return _ident("dir", dir_counter[0])

    def next_comp() -> str:
        comp_counter[0] += 1
        return _ident("comp", comp_counter[0])

    def next_file() -> str:
        file_counter[0] += 1
        return _ident("file", file_counter[0])

    def emit_dir(disk_dir: Path, directory_id: str, indent: str, is_root: bool) -> str:
        """Recursively emit a <Directory> subtree. Files in this dir become one
        component; subdirs recurse. Returns the XML for this directory's CONTENTS
        (children dirs + a <Component> if it has files)."""
        rel = disk_dir.relative_to(payload_dir)
        entries = sorted(disk_dir.iterdir(), key=lambda p: (p.is_dir(), p.name.lower()))
        files = [e for e in entries if e.is_file()]
        subdirs = [e for e in entries if e.is_dir()]

        body: list[str] = []

        if files:
            # Root component: nssm.exe must be the keypath so the service binds
            # to it; order it first.
            if is_root:
                files.sort(key=lambda p: (p.name.lower() != "nssm.exe", p.name.lower()))
            comp_id = next_comp()
            comp_rel = str(rel) if str(rel) != "." else "(root)"
            guid = _guid_for(comp_rel + "|files")
            file_xml: list[str] = []
            for i, f in enumerate(files):
                fid = next_file()
                src = f.relative_to(payload_dir).as_posix()
                keypath = ' KeyPath="yes"' if i == 0 else ""
                file_xml.append(
                    f'{indent}    <File Id="{fid}" Name={quoteattr(f.name)} '
                    f'Source={quoteattr(src)}{keypath} />'
                )
            service_xml = _service_xml(indent + "    ") if is_root else ""
            components_xml.append(
                f'{indent}  <Component Id="{comp_id}" Guid="{guid}" Win64="yes">\n'
                + "\n".join(file_xml) + "\n"
                + service_xml
                + f"{indent}  </Component>"
            )
            comp_refs.append(comp_id)
            # The component must be declared *inside* its Directory.
            body.append(_DIR_COMP_PLACEHOLDER.format(comp_id=comp_id))

        for sub in subdirs:
            sub_id = next_dir()
            inner = emit_dir(sub, sub_id, indent + "  ", is_root=False)
            body.append(
                f'{indent}  <Directory Id="{sub_id}" Name={quoteattr(sub.name)}>\n'
                f"{inner}\n"
                f"{indent}  </Directory>"
            )
        return "\n".join(body)

    # We need components nested inside their Directory elements for wixl. Build
    # the directory tree first using placeholders, then substitute the actual
    # component XML in. (Two-pass keeps the recursive emitter readable.)
    tree_body = emit_dir(payload_dir, "INSTALLDIR", "        ", is_root=True)
    comp_by_id = {}
    for cx in components_xml:
        cid = cx.split('Id="', 1)[1].split('"', 1)[0]
        comp_by_id[cid] = cx
    for cid, cx in comp_by_id.items():
        tree_body = tree_body.replace(
            _DIR_COMP_PLACEHOLDER.format(comp_id=cid), cx
        )

    # Dedicated component for the NSSM service parameters (HKLM registry).
    reg_comp_id = next_comp()
    reg_guid = _guid_for("nssm-parameters")
    reg_component = _registry_component(reg_comp_id, reg_guid, "        ")
    comp_refs.append(reg_comp_id)

    refs = "\n".join(f'      <ComponentRef Id="{c}" />' for c in comp_refs)

    return _WXS_TEMPLATE.format(
        product_name=escape(PRODUCT_NAME),
        manufacturer=escape(MANUFACTURER),
        upgrade_code=UPGRADE_CODE,
        version=escape(product_version),
        install_tree=tree_body,
        registry_component=reg_component,
        component_refs=refs,
    )


_DIR_COMP_PLACEHOLDER = "<!--COMPONENT:{comp_id}-->"


def _service_xml(indent: str) -> str:
    """ServiceInstall + ServiceControl, attached to the nssm.exe component.

    The service's binary is the component keypath (nssm.exe). NSSM, when started
    by the SCM, takes its OWN service name from argv[1] (that's why ``nssm
    install <name>`` writes ``ImagePath = "...\\nssm.exe" <name>``) and then
    reads that service's run parameters from the registry (written by the
    Registry component). So we pass the service name via ``Arguments`` -- making
    the whole thing declarative with no install-time custom action. Start is
    Wait="no" so a central server that's briefly unreachable at install time
    doesn't fail the whole installation.
    """
    return (
        f'{indent}<ServiceInstall Id="PrinterNannyService" Name="{SERVICE_NAME}"\n'
        f'{indent}    DisplayName={quoteattr(SERVICE_DISPLAY)}\n'
        f'{indent}    Description={quoteattr(SERVICE_DESC)}\n'
        f'{indent}    Arguments={quoteattr(SERVICE_NAME)}\n'
        f'{indent}    Type="ownProcess" Start="auto" ErrorControl="normal" Vital="yes" />\n'
        f'{indent}<ServiceControl Id="PrinterNannyServiceCtl" Name="{SERVICE_NAME}"\n'
        f'{indent}    Start="install" Stop="both" Remove="uninstall" Wait="no" />\n'
    )


def _registry_component(comp_id: str, guid: str, indent: str) -> str:
    """NSSM run parameters under the service's registry key.

    NSSM reads these on service start: Application = the embeddable python.exe,
    AppParameters = ``-m printer_nanny_agent --config <toml> run``, AppDirectory
    = the install dir, plus rotating stdout/stderr logging. Writing them through
    the MSI Registry table means uninstall removes them cleanly.
    """
    key = f"SYSTEM\\CurrentControlSet\\Services\\{SERVICE_NAME}\\Parameters"
    # Computed out here: Python < 3.12 forbids a backslash inside an f-string
    # expression, and this repo stays 3.9-compatible.
    appexit_key = key + "\\AppExit"
    app = "[INSTALLDIR]python\\python.exe"
    app_params = '-m printer_nanny_agent --config "[INSTALLDIR]config.toml" run'
    app_dir = "[INSTALLDIR]"
    log_path = "[INSTALLDIR]agent.log"
    return (
        f'{indent}<Component Id="{comp_id}" Guid="{guid}" Win64="yes">\n'
        # All values are fully-resolved absolute paths ([INSTALLDIR] is expanded
        # by Windows Installer at write time), so they carry no %VAR% and want
        # plain REG_SZ. (wixl emits REG_SZ for Type="string"; it does not honor
        # "expandable" anyway -- see deploy/WINDOWS-MSI-TESTING.md.)
        f'{indent}  <RegistryKey Root="HKLM" Key={quoteattr(key)}>\n'
        f'{indent}    <RegistryValue Type="string" Name="Application" '
        f'Value={quoteattr(app)} KeyPath="yes" />\n'
        f'{indent}    <RegistryValue Type="string" Name="AppParameters" '
        f'Value={quoteattr(app_params)} />\n'
        f'{indent}    <RegistryValue Type="string" Name="AppDirectory" '
        f'Value={quoteattr(app_dir)} />\n'
        f'{indent}    <RegistryValue Type="string" Name="AppStdout" '
        f'Value={quoteattr(log_path)} />\n'
        f'{indent}    <RegistryValue Type="string" Name="AppStderr" '
        f'Value={quoteattr(log_path)} />\n'
        f'{indent}    <RegistryValue Type="integer" Name="AppRotateFiles" Value="1" />\n'
        f'{indent}    <RegistryValue Type="integer" Name="AppRotateBytes" Value="5242880" />\n'
        f'{indent}    <RegistryValue Type="integer" Name="AppRestartDelay" Value="10000" />\n'
        f'{indent}  </RegistryKey>\n'
        f'{indent}  <RegistryKey Root="HKLM" Key={quoteattr(appexit_key)}>\n'
        f'{indent}    <RegistryValue Type="string" Name="Default" Value="Restart" />\n'
        f'{indent}  </RegistryKey>\n'
        f'{indent}</Component>'
    )


_WXS_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<Wix xmlns="http://schemas.microsoft.com/wix/2006/wi">
  <Product Id="*" Name="{product_name}" Language="1033" Version="{version}"
           Manufacturer="{manufacturer}" UpgradeCode="{upgrade_code}">
    <Package InstallerVersion="200" Compressed="yes" InstallScope="perMachine"
             Manufacturer="{manufacturer}" Description="{product_name} {version}" />
    <MajorUpgrade DowngradeErrorMessage="A newer version of {product_name} is already installed." />
    <Media Id="1" Cabinet="product.cab" EmbedCab="yes" />

    <Directory Id="TARGETDIR" Name="SourceDir">
      <Directory Id="ProgramFiles64Folder">
        <Directory Id="CompanyDir" Name="Printer Nanny">
          <Directory Id="INSTALLDIR" Name="Agent">
{install_tree}
{registry_component}
          </Directory>
        </Directory>
      </Directory>
    </Directory>

    <Feature Id="MainFeature" Title="{product_name}" Level="1">
{component_refs}
    </Feature>
  </Product>
</Wix>
"""


# --------------------------------------------------------------------------- #
# Top-level build
# --------------------------------------------------------------------------- #
@dataclass
class MsiBuildResult:
    path: Path
    size: int
    product_version: str
    agent_version: str
    agent_id: int


def _repo_agent_src() -> Path:
    """The agent package source dir in the repo (used as the pip install source)."""
    return Path(__file__).resolve().parents[1] / "agent"


def build_msi(
    *,
    agent_id: int,
    agent_name: str,
    central_url: str,
    api_key: str,
    verify_tls: bool = True,
    out_dir: Path,
    embed_url: Optional[str] = None,
    cache_dir: Optional[Path] = None,
    agent_src: Optional[Path] = None,
    agent_version: Optional[str] = None,
    product_version: Optional[str] = None,
    python_exe: Optional[str] = None,
) -> MsiBuildResult:
    """Build an MSI for one enrolled agent. Returns the artifact path + metadata.

    Raises ``RuntimeError`` (with actionable text) if the toolchain is missing or
    any build step fails -- the caller turns that into a dashboard flash.
    """
    cap = msi_build_available()
    if not cap.available:
        raise RuntimeError(cap.reason)

    cache = Path(cache_dir) if cache_dir else _cache_dir()
    embed_url = embed_url or os.environ.get("PN_PYTHON_EMBED_URL") or DEFAULT_PYTHON_EMBED_URL
    agent_src = Path(agent_src) if agent_src else _repo_agent_src()
    python_exe = python_exe or sys.executable
    if agent_version is None:
        from central.agent_release import bundled_agent_version

        agent_version = bundled_agent_version()
    if product_version is None:
        from central import __version__ as product_version  # type: ignore

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    runtime = _ensure_runtime_tree(
        cache=cache, embed_url=embed_url, agent_src=agent_src,
        agent_version=agent_version, python_exe=python_exe,
    )

    # Assemble a per-agent payload: the cached runtime + a fresh config.toml.
    payload = out_dir / f"payload-{agent_id}"
    if payload.exists():
        shutil.rmtree(payload)
    shutil.copytree(runtime, payload)
    (payload / "config.toml").write_text(
        render_config_toml(
            central_url=central_url, agent_id=agent_id,
            api_key=api_key, verify_tls=verify_tls,
        )
    )

    # The .wxs lives inside the payload dir so wixl (run with cwd=payload)
    # resolves every File @Source by its path relative to the install tree. The
    # payload holds config.toml with the plaintext key, so clean it in a finally
    # whether wixl succeeds or fails -- the only surviving copy is the MSI in
    # out_dir, which the caller owns.
    msi_path = out_dir / f"printer-nanny-agent-{agent_id}.msi"
    try:
        wxs = generate_wxs(payload, product_version=product_version)
        wxs_name = f"agent-{agent_id}.wxs"
        (payload / wxs_name).write_text(wxs)

        cmd = [cap.wixl or "wixl", "--arch", "x64", "-o", str(msi_path), wxs_name]
        log.info("running wixl: %s (cwd=%s)", " ".join(cmd), payload)
        proc = subprocess.run(cmd, cwd=str(payload), capture_output=True, text=True)
        if proc.returncode != 0 or not msi_path.exists():
            raise RuntimeError(
                "wixl failed to build the MSI:\n"
                + proc.stdout[-2000:] + "\n" + proc.stderr[-2000:]
            )
    finally:
        shutil.rmtree(payload, ignore_errors=True)

    return MsiBuildResult(
        path=msi_path,
        size=msi_path.stat().st_size,
        product_version=product_version,
        agent_version=agent_version,
        agent_id=agent_id,
    )


# --------------------------------------------------------------------------- #
# Validation (used by tests + an optional post-build sanity check)
# --------------------------------------------------------------------------- #
def validate_msi(path: Path) -> dict:
    """Inspect a built MSI with msitools and return a structured summary.

    Confirms the structural promises the Windows install relies on: the standard
    tables exist, a service is registered, the NSSM Application points at the
    bundled python.exe, and the injected config.toml carries this enrollment.
    """
    path = Path(path)
    tables = _msiinfo_tables(path)
    summary: dict = {"tables": tables}

    summary["has_service"] = "ServiceInstall" in tables and "ServiceControl" in tables
    summary["has_registry"] = "Registry" in tables
    summary["has_files"] = "File" in tables

    registry = _msiinfo_export(path, "Registry") if "Registry" in tables else ""
    summary["nssm_application_ok"] = (
        "Application" in registry and "python.exe" in registry
    )
    summary["nssm_appparameters_ok"] = (
        "AppParameters" in registry and "printer_nanny_agent" in registry
    )

    # Extract the bundled config.toml to confirm the enrollment was injected.
    config = _extract_member(path, "config.toml")
    summary["config_toml"] = config
    return summary


def _msiinfo_tables(path: Path) -> list[str]:
    proc = subprocess.run(
        ["msiinfo", "tables", str(path)], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise RuntimeError(f"msiinfo tables failed: {proc.stderr}")
    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]


def _msiinfo_export(path: Path, table: str) -> str:
    proc = subprocess.run(
        ["msiinfo", "export", str(path), table], capture_output=True, text=True
    )
    return proc.stdout if proc.returncode == 0 else ""


def _extract_member(path: Path, name: str) -> Optional[str]:
    """msiextract the MSI to a temp dir and return the text of ``name`` if found."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        proc = subprocess.run(
            ["msiextract", "-C", td, str(path)], capture_output=True, text=True
        )
        if proc.returncode != 0:
            return None
        for found in Path(td).rglob(name):
            try:
                return found.read_text()
            except OSError:
                return None
    return None
