"""The api container must not throw its own logs away.

`central.worker.run.main()` called `logging.basicConfig`, so the *worker*
container logged. `central.main` configured logging nowhere, so in the *api*
container every `log.info()` under `central/` went to the floor and every
warning came out through `logging.lastResort` -- bare `%(message)s`, no
timestamp, no level, no logger name. Everything built afterwards was debugged
through logs that went nowhere.

These tests assert on captured *output*, never on "was basicConfig called":
the failure being guarded against is precisely a configuration call that runs
and still emits nothing. Two of them run in a **subprocess**, because inside
pytest the root logger is already carrying the capture plugin's handlers and a
fresh interpreter is the only place the container's real starting conditions
exist -- and one of those deliberately reproduces the old broken state, so the
suite would fail if the fix were reverted.
"""

from __future__ import annotations

import io
import logging
import os
import pathlib
import re
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from central import logging_config as lc
from central.main import app

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# 2026-08-03T11:22:33+0000 INFO central.thing: message
LINE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4} (?P<level>[A-Z]+) (?P<name>\S+): (?P<msg>.*)$"
)


# --------------------------------------------------------------------------- #
# A fresh interpreter: exactly what the api container starts with.
# --------------------------------------------------------------------------- #

def _run(body: str, tmp_path, **env_overrides) -> subprocess.CompletedProcess:
    """Run `body` in a fresh interpreter rooted at THIS checkout.

    PYTHONPATH is pinned to the repo root on purpose. The editable install
    resolves `central` against whichever checkout pip was pointed at, and cwd is
    the only other thing putting this one on sys.path -- so a subprocess test
    that skipped this would happily verify some other worktree's code. The body
    prints the file it actually imported and the caller checks it.
    """
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "DATABASE_URL": f"sqlite:///{tmp_path / 'subprocess.sqlite3'}",
        "SECRET_KEY": "test-secret",
    }
    # A LOG_LEVEL exported in the developer's own shell must not decide what
    # these assert; only the test says what the level is.
    env.pop("LOG_LEVEL", None)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", body],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=180,
    )


_IMPORT_APP = """
import inspect, logging
import central
import central.main
print("IMPORTED", inspect.getfile(central), flush=True)
logging.getLogger("central.smoke").info("hello-from-central")
logging.getLogger("central.smoke").warning("warned-from-central")
"""


def test_importing_the_app_makes_central_info_reach_stderr(tmp_path):
    """The whole point: a log.info() under central/ must actually come out."""
    proc = _run(_IMPORT_APP, tmp_path)
    assert proc.returncode == 0, proc.stderr

    assert str(REPO_ROOT) in proc.stdout, f"imported the wrong checkout: {proc.stdout!r}"

    lines = [LINE.match(ln) for ln in proc.stderr.splitlines() if "from-central" in ln]
    assert len(lines) == 2, f"expected the INFO and the WARNING, got: {proc.stderr!r}"
    info, warned = lines
    assert info is not None and warned is not None, proc.stderr
    assert (info["level"], info["name"], info["msg"]) == \
        ("INFO", "central.smoke", "hello-from-central")
    assert (warned["level"], warned["name"]) == ("WARNING", "central.smoke")


def test_without_the_app_the_same_record_is_lost(tmp_path):
    """The control. Without configure_logging this is what the api container did.

    Importing a `central` module that is not the app leaves logging unconfigured,
    so the INFO vanishes and the WARNING arrives through `logging.lastResort`
    with no timestamp, level or logger name. If this ever starts passing the
    formatted shape, the test above has stopped proving anything.
    """
    body = _IMPORT_APP.replace("import central.main\n", "")
    proc = _run(body, tmp_path)
    assert proc.returncode == 0, proc.stderr

    assert "hello-from-central" not in proc.stderr, "INFO survived without configuration?"
    assert "warned-from-central" in proc.stderr
    assert LINE.match(proc.stderr.strip().splitlines()[-1]) is None, \
        "lastResort should emit a bare message, not a formatted line"


_UNDER_UVICORN = """
import inspect, logging, logging.config
from uvicorn.config import LOGGING_CONFIG
# Exactly what `uvicorn central.main:app` does: Config.__init__ applies this
# dictConfig, and only then does Config.load() import the app.
logging.config.dictConfig(LOGGING_CONFIG)
import central
import central.main
print("IMPORTED", inspect.getfile(central), flush=True)
logging.getLogger("central.smoke").info("hello-from-central")
logging.getLogger("uvicorn.error").info("hello-from-uvicorn")
"""


def test_uvicorn_logging_and_ours_coexist(tmp_path):
    """Under uvicorn both must be heard, each exactly once.

    uvicorn's config names only the `uvicorn*` loggers and leaves root alone, and
    marks `uvicorn`/`uvicorn.access` non-propagating -- so our root handler is
    neither clobbered by it nor a source of doubled uvicorn lines. Asserted
    rather than assumed, because a uvicorn release that grew a `root` key would
    silently take the api container's logs away again.
    """
    proc = _run(_UNDER_UVICORN, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert str(REPO_ROOT) in proc.stdout, f"imported the wrong checkout: {proc.stdout!r}"

    ours = [ln for ln in proc.stderr.splitlines() if "hello-from-central" in ln]
    theirs = [ln for ln in proc.stderr.splitlines() if "hello-from-uvicorn" in ln]
    assert len(ours) == 1, f"our record should appear once: {proc.stderr!r}"
    assert len(theirs) == 1, f"uvicorn's record should appear once: {proc.stderr!r}"
    assert LINE.match(ours[0]), f"our line lost its format under uvicorn: {ours[0]!r}"


def test_log_level_is_operator_controllable(tmp_path):
    """LOG_LEVEL is a bootstrap concern, so it comes from the environment."""
    quiet = _run(_IMPORT_APP, tmp_path, LOG_LEVEL="WARNING")
    assert quiet.returncode == 0, quiet.stderr
    assert "hello-from-central" not in quiet.stderr
    assert "warned-from-central" in quiet.stderr

    loud = _run(_IMPORT_APP.replace('.info("hello', '.debug("hello'), tmp_path,
                LOG_LEVEL="debug")
    assert loud.returncode == 0, loud.stderr
    assert "hello-from-central" in loud.stderr, "lower-case level name not honoured"


def test_a_bogus_log_level_does_not_take_the_container_down(tmp_path):
    """A typo in .env must cost a preference, never a boot."""
    proc = _run(_IMPORT_APP, tmp_path, LOG_LEVEL="verbose")
    assert proc.returncode == 0, proc.stderr
    assert "hello-from-central" in proc.stderr, "should have fallen back to INFO"
    assert "is not a log level" in proc.stderr, "the bad value should be reported"


# --------------------------------------------------------------------------- #
# In-process: the app's own startup record, and the level policy.
# --------------------------------------------------------------------------- #

@pytest.fixture()
def log_buffer():
    """Point the project's handler at a buffer, and put everything back after."""
    root = logging.getLogger()
    handlers = list(root.handlers)
    root_level = root.level
    levels = {name: logging.getLogger(name).level for name in lc.PROJECT_LOGGERS}
    ours = next((h for h in root.handlers if h.get_name() == lc.HANDLER_NAME), None)
    stream = getattr(ours, "stream", None)
    formatter = getattr(ours, "formatter", None)

    buf = io.StringIO()
    try:
        yield buf
    finally:
        root.handlers[:] = handlers
        root.setLevel(root_level)
        for name, level in levels.items():
            logging.getLogger(name).setLevel(level)
        if ours is not None:
            ours.setStream(stream)
            ours.setFormatter(formatter)


def test_app_startup_emits_a_formatted_record(log_buffer):
    """Real app setup, real record: the lifespan's own line lands, formatted."""
    lc.configure_logging(stream=log_buffer)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200

    matches = [LINE.match(ln) for ln in log_buffer.getvalue().splitlines()]
    starting = [m for m in matches if m and "starting" in m["msg"]]
    assert starting, f"no formatted startup record: {log_buffer.getvalue()!r}"
    assert starting[0]["level"] == "INFO"
    assert starting[0]["name"].startswith("central.")
    assert app.version in starting[0]["msg"]


def test_configure_logging_is_idempotent(log_buffer):
    root = logging.getLogger()
    lc.configure_logging(stream=log_buffer)
    before = [h for h in root.handlers if h.get_name() == lc.HANDLER_NAME]
    lc.configure_logging(stream=log_buffer)
    after = [h for h in root.handlers if h.get_name() == lc.HANDLER_NAME]
    assert len(before) == len(after) == 1

    logging.getLogger("central.dupe").info("once")
    assert log_buffer.getvalue().count("once") == 1


def test_the_level_knob_never_reaches_a_dependency(log_buffer):
    """Turning our verbosity up must not turn a library's on.

    httpx logs the full request URL at INFO and a Slack / Teams / generic-webhook
    URL *is* the credential; SQLAlchemy logs bound parameters at DEBUG, which is
    password hashes and Fernet ciphertext. So the knob moves this project's
    loggers and leaves the root logger's level exactly where it found it.
    """
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    lc.configure_logging(level="DEBUG", stream=log_buffer)

    assert root.level == logging.WARNING, "the root logger's level must not move"

    logging.getLogger("central.db").debug("ours-at-debug")
    logging.getLogger("printer_nanny.worker").info("ours-at-info")
    logging.getLogger("httpx").info("HTTP Request: POST https://hooks.slack.test/T/B/secret")
    logging.getLogger("sqlalchemy.engine.Engine").debug("SELECT ... ('hashed-password',)")
    logging.getLogger("httpx").warning("a library warning is still heard")

    out = log_buffer.getvalue()
    assert "ours-at-debug" in out
    assert "ours-at-info" in out
    assert "hooks.slack.test" not in out, "a dependency's INFO leaked a webhook URL"
    assert "hashed-password" not in out, "a dependency's DEBUG leaked bound parameters"
    assert "a library warning is still heard" in out


def test_settings_supply_the_default_level(log_buffer, monkeypatch):
    monkeypatch.setattr(lc.settings, "log_level", "WARNING")
    lc.configure_logging(stream=log_buffer)
    logging.getLogger("central.quiet").info("suppressed")
    logging.getLogger("central.quiet").warning("kept")
    out = log_buffer.getvalue()
    assert "suppressed" not in out
    assert "kept" in out


@pytest.mark.parametrize(
    "value,expected",
    [
        ("DEBUG", logging.DEBUG),
        ("info", logging.INFO),
        (" Warning ", logging.WARNING),
        ("WARN", logging.WARNING),
        ("40", logging.ERROR),
        (logging.ERROR, logging.ERROR),
        # Rejected, each for its own reason: unknown name, empty, unset, a bool
        # (which is an int subclass and would otherwise mean level 1), and
        # NOTSET -- which on these loggers means "inherit", i.e. silently back to
        # the bug this module exists to fix.
        ("verbose", logging.INFO),
        ("", logging.INFO),
        (None, logging.INFO),
        (True, logging.INFO),
        ("NOTSET", logging.INFO),
        ("0", logging.INFO),
    ],
)
def test_resolve_level(value, expected):
    assert lc.resolve_level(value) == expected
