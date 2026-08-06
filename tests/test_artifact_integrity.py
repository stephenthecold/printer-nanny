"""Integrity of the third-party binaries that get baked into an installer.

Two artifacts are fetched from the internet and shipped, unmodified, to customer
workstations: the Python embeddable runtime and NSSM. NSSM is the *service host*
-- the process that runs the workstation agent as LocalSystem -- so a substituted
binary is SYSTEM code execution across a client's fleet. Before
``central.artifact_integrity`` the only check on either was a size floor, and the
cache was trusted for existing.

These tests pin the three properties that fix relies on: a configured digest is
enforced, an unconfigured one is pinned on arrival and enforced from then on, and
plaintext transport is refused outright.
"""

from __future__ import annotations

import hashlib

import pytest

from central import artifact_integrity as ai


# --------------------------------------------------------------------------- #
# Digest plumbing
# --------------------------------------------------------------------------- #
def test_file_and_bytes_digests_agree(tmp_path):
    """The streaming file digest must equal the in-memory one.

    They are used interchangeably -- bytes are verified at download, the file is
    verified on every later read -- so a divergence would reject every cached
    artifact on its second use.
    """
    data = b"nssm" * 5000
    path = tmp_path / "nssm.exe"
    path.write_bytes(data)
    assert ai.sha256_file(path) == ai.sha256_bytes(data) == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("bad", ["deadbeef", "z" * 64, "abc123", " " * 3 + "x"])
def test_a_malformed_pin_raises_rather_than_being_ignored(bad):
    """An operator who typed a broken checksum must be told it did not apply.

    Silently treating a malformed pin as "no pin" is how a control ends up
    believed-in and absent.
    """
    with pytest.raises(ai.ArtifactIntegrityError):
        ai.normalise_pin(bad)


def test_an_absent_pin_is_none_not_an_error():
    assert ai.normalise_pin(None) is None
    assert ai.normalise_pin("") is None
    assert ai.normalise_pin("   ") is None


def test_a_pin_is_case_normalised():
    digest = hashlib.sha256(b"x").hexdigest()
    assert ai.normalise_pin(digest.upper()) == digest


# --------------------------------------------------------------------------- #
# Enforcement
# --------------------------------------------------------------------------- #
def test_a_configured_pin_is_enforced_on_bytes():
    data = b"the real runtime"
    wrong = hashlib.sha256(b"something else").hexdigest()
    with pytest.raises(ai.ArtifactIntegrityError) as exc:
        ai.verify_bytes(data, expected=wrong, label="the runtime")
    # The message must name both digests: an operator diagnosing this needs to
    # know whether upstream moved or their pin is stale.
    assert wrong in str(exc.value)
    assert ai.sha256_bytes(data) in str(exc.value)


def test_a_matching_pin_passes_and_returns_the_digest():
    data = b"the real runtime"
    digest = ai.sha256_bytes(data)
    assert ai.verify_bytes(data, expected=digest, label="the runtime") == digest


def test_first_use_pins_and_the_second_use_is_verified(tmp_path):
    """The TOFU half: nothing authenticates the first fetch, but every later read
    is checked against what the first one recorded."""
    path = tmp_path / "python-embed.zip"
    path.write_bytes(b"original bytes")
    first = ai.verify_or_pin_file(path, label="the runtime")
    assert ai.pin_path(path).exists()
    # Unchanged file: still fine.
    assert ai.verify_or_pin_file(path, label="the runtime") == first
    # Tampered on the cache volume: caught.
    path.write_bytes(b"swapped bytes")
    with pytest.raises(ai.ArtifactIntegrityError):
        ai.verify_or_pin_file(path, label="the runtime")


def test_a_configured_pin_beats_the_sidecar(tmp_path):
    """An operator's pin is authoritative. If the sidecar were preferred, an
    attacker who could write the cache could also write the sidecar and the pin
    would never be consulted."""
    path = tmp_path / "nssm.exe"
    path.write_bytes(b"tampered")
    ai.write_pin(path, ai.sha256_bytes(b"tampered"))  # attacker-consistent pair
    with pytest.raises(ai.ArtifactIntegrityError):
        ai.verify_or_pin_file(
            path, expected=hashlib.sha256(b"genuine").hexdigest(), label="nssm.exe"
        )


def test_a_corrupt_sidecar_is_an_error_not_an_absent_pin(tmp_path):
    """Otherwise corrupting the sidecar is how you turn verification off."""
    path = tmp_path / "nssm.exe"
    path.write_bytes(b"payload")
    ai.pin_path(path).write_text("not a digest\n", encoding="ascii")
    with pytest.raises(ai.ArtifactIntegrityError):
        ai.verify_or_pin_file(path, label="nssm.exe")


def test_allow_pin_false_neither_writes_nor_reads_a_sidecar(tmp_path):
    """Operator-staged files live outside our cache; we do not litter sidecars
    next to them, and their absence must not be read as a verification failure."""
    path = tmp_path / "staged.zip"
    path.write_bytes(b"operator staged this")
    ai.verify_or_pin_file(path, label="the runtime", allow_pin=False)
    assert not ai.pin_path(path).exists()


def test_discard_removes_the_artifact_and_its_pin(tmp_path):
    """A rejected file must not survive to be picked up by the next build."""
    path = tmp_path / "nssm.exe"
    path.write_bytes(b"bad")
    ai.write_pin(path, ai.sha256_bytes(b"bad"))
    ai.discard(path)
    assert not path.exists()
    assert not ai.pin_path(path).exists()
    ai.discard(path)  # idempotent: a second discard must not raise


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url", ["http://nssm.cc/release/nssm-2.24.zip", "http://mirror.internal/python.zip"]
)
def test_plaintext_remote_urls_are_refused(url):
    """Both URLs are operator-overridable free text with no scheme restriction,
    so http:// used to be accepted for a binary installed as a service."""
    with pytest.raises(ai.ArtifactIntegrityError):
        ai.require_secure_url(url, label="the runtime")


@pytest.mark.parametrize(
    "url",
    [
        "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip",
        "file:///srv/artifacts/python-embed.zip",
        "/srv/artifacts/python-embed.zip",
        "C:\\artifacts\\python-embed.zip",
    ],
)
def test_https_and_local_paths_are_accepted(url):
    """Air-gapped installs stage artifacts locally; that path must keep working."""
    assert ai.require_secure_url(url, label="the runtime") == url


def test_a_downgrading_redirect_is_refused():
    """Checking the configured URL is not enough -- redirects are followed, so an
    https mirror answering `302 Location: http://...` would otherwise downgrade
    the fetch after the check had already passed."""
    with pytest.raises(ai.ArtifactIntegrityError):
        ai.assert_secure_response_url("http://evil.example/nssm.zip", label="nssm")


def test_a_redirect_that_stays_https_is_fine():
    ai.assert_secure_response_url("https://cdn.example/nssm.zip", label="nssm")
