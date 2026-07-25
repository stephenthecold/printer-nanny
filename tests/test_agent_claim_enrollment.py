"""Agent-side claim-code enrollment.

The failure that matters most here is not a rejected code -- it is redeeming
successfully and then losing the answer. A claim code is single use, so an agent
that enrolls and fails to persist its credentials is permanently broken, and the
operator has to mint a new code to recover. Most of these tests are about that.
"""

from __future__ import annotations

import json
import os
import stat
from urllib import error as urlerror

import pytest

from printer_nanny_agent import config as agent_config
from printer_nanny_agent import enrollment


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_urlopen(monkeypatch, payload=None, error=None, captured=None):
    def fake_urlopen(req, timeout=None, context=None):
        if captured is not None:
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
        if error is not None:
            raise error
        return _FakeResponse(payload)

    monkeypatch.setattr(enrollment.urlrequest, "urlopen", fake_urlopen)


# ---------- redemption ----------

def test_redeem_posts_the_code_and_returns_credentials(monkeypatch):
    captured: dict = {}
    _patch_urlopen(
        monkeypatch,
        payload={"agent_id": 7, "api_key": "pn_secret", "site_id": 2, "name": "a"},
        captured=captured,
    )
    creds = enrollment.redeem_claim_code("https://central.example/", "pnc_abc")
    assert creds["agent_id"] == 7 and creds["api_key"] == "pn_secret"
    assert captured["url"] == "https://central.example/api/v1/agents/register"
    assert captured["body"]["claim_code"] == "pnc_abc"
    # A hostname is sent, but it is central's job to treat it as a label.
    assert "hostname" in captured["body"]


def test_rejected_code_gives_an_actionable_error(monkeypatch):
    _patch_urlopen(monkeypatch, error=urlerror.HTTPError(
        "u", 401, "Unauthorized", {}, None))
    with pytest.raises(enrollment.EnrollmentError) as exc:
        enrollment.redeem_claim_code("https://c", "pnc_bad")
    assert "expired" in str(exc.value) and "mint a fresh one" in str(exc.value).lower()


def test_unreachable_central_is_reported_not_swallowed(monkeypatch):
    _patch_urlopen(monkeypatch, error=urlerror.URLError("connection refused"))
    with pytest.raises(enrollment.EnrollmentError) as exc:
        enrollment.redeem_claim_code("https://c", "pnc_x")
    assert "could not reach" in str(exc.value)


def test_a_response_missing_credentials_is_refused(monkeypatch):
    """Better to fail loudly than to write half an identity to disk."""
    _patch_urlopen(monkeypatch, payload={"agent_id": 7})
    with pytest.raises(enrollment.EnrollmentError) as exc:
        enrollment.redeem_claim_code("https://c", "pnc_x")
    assert "api_key" in str(exc.value)


# ---------- persistence ----------

def test_credentials_are_written_and_readable_back(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text('central_url = "https://c"\nclaim_code = "pnc_used"\n')
    enrollment.persist_credentials(str(path), {"agent_id": 12, "api_key": "pn_k"})

    cfg = agent_config.load_config(str(path))
    assert cfg.agent_id == 12 and cfg.api_key == "pn_k"


def test_the_spent_claim_code_is_removed_from_the_file(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text('central_url = "https://c"\nclaim_code = "pnc_used"\n')
    enrollment.persist_credentials(str(path), {"agent_id": 1, "api_key": "pn_k"})
    assert "claim_code" not in path.read_text()


def test_rewriting_does_not_leave_two_conflicting_identities(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text('central_url = "https://c"\nagent_id = 1\napi_key = "old"\n')
    enrollment.persist_credentials(str(path), {"agent_id": 2, "api_key": "new"})
    body = path.read_text()
    assert body.count("agent_id") == 1 and body.count("api_key") == 1
    assert "old" not in body


def test_other_settings_survive_the_rewrite(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text(
        'central_url = "https://c"\nverify_tls = false\ndata_dir = "/var/tmp/pn"\n'
    )
    enrollment.persist_credentials(str(path), {"agent_id": 3, "api_key": "pn_k"})
    cfg = agent_config.load_config(str(path))
    assert cfg.central_url == "https://c"
    assert cfg.verify_tls is False
    assert cfg.data_dir == "/var/tmp/pn"


def test_the_credential_file_is_owner_only(tmp_path):
    path = tmp_path / "agent.toml"
    enrollment.persist_credentials(str(path), {"agent_id": 4, "api_key": "pn_k"})
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"config holding an API key is mode {mode:o}"


def test_no_temp_file_is_left_behind(tmp_path):
    path = tmp_path / "agent.toml"
    enrollment.persist_credentials(str(path), {"agent_id": 5, "api_key": "pn_k"})
    assert [p.name for p in tmp_path.iterdir()] == ["agent.toml"]


# ---------- the load_config integration ----------

def test_claim_code_in_config_enrolls_and_persists(tmp_path, monkeypatch):
    _patch_urlopen(monkeypatch, payload={"agent_id": 9, "api_key": "pn_new"})
    path = tmp_path / "agent.toml"
    path.write_text('central_url = "https://c"\nclaim_code = "pnc_go"\n')

    cfg = agent_config.load_config(str(path))
    assert cfg.agent_id == 9 and cfg.api_key == "pn_new"
    # Persisted, so the (single-use) code is never needed again.
    assert "pn_new" in path.read_text()
    assert "claim_code" not in path.read_text()


def test_second_start_does_not_redeem_again(tmp_path, monkeypatch):
    calls = {"n": 0}

    def counting_urlopen(req, timeout=None, context=None):
        calls["n"] += 1
        return _FakeResponse({"agent_id": 9, "api_key": "pn_new"})

    monkeypatch.setattr(enrollment.urlrequest, "urlopen", counting_urlopen)
    path = tmp_path / "agent.toml"
    path.write_text('central_url = "https://c"\nclaim_code = "pnc_go"\n')

    agent_config.load_config(str(path))
    agent_config.load_config(str(path))
    assert calls["n"] == 1, "re-redeemed a single-use code on restart"


def test_existing_credentials_beat_a_pasted_claim_code(tmp_path, monkeypatch):
    def explode(req, timeout=None, context=None):  # pragma: no cover
        raise AssertionError("must not attempt enrollment when already enrolled")

    monkeypatch.setattr(enrollment.urlrequest, "urlopen", explode)
    path = tmp_path / "agent.toml"
    path.write_text(
        'central_url = "https://c"\nagent_id = 3\napi_key = "pn_have"\n'
        'claim_code = "pnc_stray"\n'
    )
    cfg = agent_config.load_config(str(path))
    assert cfg.agent_id == 3 and cfg.api_key == "pn_have"


def test_claim_code_without_central_url_fails_clearly(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text('claim_code = "pnc_go"\n')
    with pytest.raises(ValueError) as exc:
        agent_config.load_config(str(path))
    assert "central_url" in str(exc.value)


def test_claim_code_from_the_environment_works(tmp_path, monkeypatch):
    """What the one-line installer actually relies on."""
    _patch_urlopen(monkeypatch, payload={"agent_id": 11, "api_key": "pn_env"})
    path = tmp_path / "agent.toml"
    monkeypatch.setenv("PN_CENTRAL_URL", "https://c")
    monkeypatch.setenv("PN_CLAIM_CODE", "pnc_env")
    cfg = agent_config.load_config(str(path))
    assert cfg.agent_id == 11 and cfg.api_key == "pn_env"
