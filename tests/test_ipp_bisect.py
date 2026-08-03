"""The search logic in scripts/ipp_bisect.py.

The verdict for a trial needs a real Windows box rendering to a real printer, so
the oracle cannot be tested here. Everything above that seam can be, and this is
the file that does it -- against synthetic oracles whose answer is known, which
is the only way to tell a search that works from one that merely terminates.

The load-bearing test is `test_bisection_halts_where_ddmin_succeeds`. The
recorded evidence about the Brother is that neither half of the 78 donor
attributes is sufficient alone, and that is exactly the input on which binary
search halts with no answer. If that property ever stops holding for this
implementation, the module docstring's claim -- that the obvious approach cannot
work here and delta debugging can -- has stopped being true.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import struct
import types

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "ipp_bisect.py"


def _load():
    spec = importlib.util.spec_from_file_location("ipp_bisect", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ib = _load()


# --- helpers ----------------------------------------------------------------
def _ipp(names, group=0x04):
    """A minimal IPP response body carrying `names` in the given group."""
    out = struct.pack(">BBHI", 2, 0, 0, 1)
    out += bytes([0x01])  # operation attrs
    out += bytes([group])
    for name in names:
        nb, vb = name.encode(), b"x"
        out += struct.pack(">BH", 0x44, len(nb)) + nb + struct.pack(">H", len(vb)) + vb
    out += bytes([0x03])  # end
    return out


def oracle_for(required):
    """A test function that PASSes only when every name in `required` is present."""
    required = set(required)
    calls = []

    def test(subset):
        calls.append(list(subset))
        return ib.PASS if required.issubset(set(subset)) else ib.FAIL

    test.calls = calls
    return test


# --- ddmin finds interacting causes -----------------------------------------
def test_finds_a_single_responsible_attribute():
    names = [f"a{i}" for i in range(16)]
    got = ib.ddmin(names, oracle_for({"a7"}))
    assert got == ["a7"]


def test_finds_a_two_way_interaction():
    """The shape the evidence describes: two attributes that only work together."""
    names = [f"a{i}" for i in range(16)]
    got = ib.ddmin(names, oracle_for({"a2", "a11"}))
    assert sorted(got) == ["a11", "a2"]


def test_finds_a_three_way_interaction():
    names = [f"a{i}" for i in range(24)]
    got = ib.ddmin(names, oracle_for({"a1", "a9", "a20"}))
    assert sorted(got) == ["a1", "a20", "a9"]


def test_result_is_one_minimal():
    """Removing any single element must lose the effect. That is the guarantee."""
    names = [f"a{i}" for i in range(20)]
    test = oracle_for({"a3", "a14"})
    got = ib.ddmin(names, test)
    assert test(got) == ib.PASS
    for name in got:
        assert test([x for x in got if x != name]) == ib.FAIL


def test_the_78_attribute_case_converges():
    """The real search space, with a cause a bisect cannot see."""
    names = [f"attr-{i:02d}" for i in range(78)]
    test = oracle_for({"attr-05", "attr-61"})
    got = ib.ddmin(names, test)
    assert sorted(got) == ["attr-05", "attr-61"]


def test_the_trial_budget_is_survivable_on_real_hardware():
    """A trial is a physical print job, so the count is the whole feasibility question.

    Measured over 40 random placements in 78 attributes: median 38 distinct
    subsets for a two-attribute cause (p90 44, max 47), ~70 for three. At a
    couple of minutes a trial that is an afternoon, which is why this approach is
    worth running at all. The bound below is loose enough not to be brittle and
    tight enough that a change making the search an order of magnitude more
    expensive fails here rather than on someone's Friday.
    """
    names = [f"attr-{i:02d}" for i in range(78)]
    required = {"attr-05", "attr-61"}
    distinct = {}

    def test(subset):
        key = tuple(sorted(subset))
        if key not in distinct:
            distinct[key] = ib.PASS if required.issubset(set(subset)) else ib.FAIL
        return distinct[key]

    ib.ddmin(names, test)
    assert len(distinct) < 80, f"{len(distinct)} physical print jobs is too many"


def test_memoising_roughly_halves_the_print_jobs():
    """Which is why the journal exists rather than being a convenience."""
    names = [f"attr-{i:02d}" for i in range(78)]
    test = oracle_for({"attr-05", "attr-61"})
    ib.ddmin(names, test)
    distinct = {tuple(sorted(c)) for c in test.calls}
    assert len(distinct) < len(test.calls) * 0.75


def test_survives_a_non_monotone_oracle():
    """Adding a donor attribute might REINTRODUCE the fault, not only fix it.

    ddmin's guarantees are usually stated for monotone predicates, and there is
    no reason to believe this one is monotone -- so the property that matters is
    checked directly: whatever comes back must actually pass, and must be
    1-minimal. Measured 30/30 across random placements.
    """
    names = [f"a{i:02d}" for i in range(40)]
    need, breaker = {"a07", "a23"}, "a31"

    def test(subset):
        s = set(subset)
        return ib.PASS if need.issubset(s) and breaker not in s else ib.FAIL

    got = ib.ddmin(names, test)
    assert test(got) == ib.PASS
    for name in got:
        assert test([x for x in got if x != name]) == ib.FAIL


def test_a_breaker_attribute_is_caught_by_the_premise_check_not_the_search():
    """If some attribute breaks the effect, importing everything does NOT print.

    That is the premise `main()` verifies before spending hours, and it is the
    signal that the donor, the device or the oracle is not what the recorded
    evidence described. Asserted here so the sanity check is understood as load
    bearing rather than ceremony.
    """
    names = [f"a{i:02d}" for i in range(40)]

    def test(subset):
        s = set(subset)
        return ib.PASS if {"a07"}.issubset(s) and "a31" not in s else ib.FAIL

    assert test(names) == ib.FAIL


# --- the claim in the docstring, made checkable ------------------------------
def test_bisection_halts_where_ddmin_succeeds():
    """Both halves negative is where binary search stops and ddmin keeps going.

    This is the whole argument for the algorithm choice, so it is asserted rather
    than left as prose. The two required attributes are placed in opposite halves
    deliberately -- that is what the recorded "neither half is sufficient" result
    means.
    """
    names = [f"a{i}" for i in range(16)]
    required = {"a1", "a12"}  # a1 in the first half, a12 in the second

    assert ib.bisect_would_halt(names, oracle_for(required)) is True
    assert sorted(ib.ddmin(names, oracle_for(required))) == ["a1", "a12"]


def test_bisection_would_have_worked_for_a_lone_cause():
    """And it does not halt when the cause is single -- so the test above is
    detecting an interaction, not a broken helper."""
    names = [f"a{i}" for i in range(16)]
    assert ib.bisect_would_halt(names, oracle_for({"a3"})) is False


# --- an inconclusive trial must never become a verdict -----------------------
def test_no_verdict_without_a_query():
    """QUERIES: 0 means Windows answered from its device cache."""
    verdict, queries, _ = ib.parse_verdict("VERDICT: PASS\nQUERIES: 0\nIDENTITY: X\n")
    assert verdict == ib.INDETERMINATE
    assert queries == 0


def test_no_verdict_from_a_server_we_did_not_start():
    """The stale-server trap: the old process kept the port and answered instead.

    `pkill` + `sleep 1` leaves the previous configuration serving when its
    replacement fails to bind, and that config prints or does not print for its
    own reasons. The verdict is about attributes nobody selected.
    """
    verdict, _, identity = ib.parse_verdict(
        "VERDICT: PASS\nQUERIES: 9\nIDENTITY: Bisect old-trial\n",
        expect_identity="Bisect this-trial",
    )
    assert verdict == ib.INDETERMINATE
    assert identity == "Bisect old-trial", "the observed identity survives, for diagnosis"


def test_the_expected_identity_lets_a_verdict_stand():
    assert ib.parse_verdict(
        "VERDICT: PASS\nQUERIES: 9\nIDENTITY: Bisect this-trial\n",
        expect_identity="Bisect this-trial",
    )[0] == ib.PASS


def test_a_silent_oracle_does_not_pass_the_identity_check():
    """Omitting IDENTITY must not be cheaper than reporting a wrong one."""
    assert ib.parse_verdict(
        "VERDICT: PASS\nQUERIES: 9\n", expect_identity="Bisect this-trial",
    )[0] == ib.INDETERMINATE


def test_a_pass_with_queries_stands():
    verdict, queries, identity = ib.parse_verdict(
        "noise\nVERDICT: PASS\nQUERIES: 8\nIDENTITY: Bisect Device\n"
    )
    assert (verdict, queries, identity) == (ib.PASS, 8, "Bisect Device")


def test_unparseable_output_is_not_a_failure():
    """The bug that invalidated the earlier bisect: silence read as evidence."""
    assert ib.parse_verdict("")[0] == ib.INDETERMINATE
    assert ib.parse_verdict("the script crashed")[0] == ib.INDETERMINATE
    assert ib.parse_verdict("VERDICT: nonsense\nQUERIES: 4\n")[0] == ib.INDETERMINATE


def test_indeterminate_aborts_rather_than_guessing():
    journal = ib.Journal(None)
    test = ib.make_test("true", journal, retries=1, log=lambda *a: None)
    # `true` produces no VERDICT line at all, so every attempt is inconclusive.
    with pytest.raises(ib.Indeterminate):
        test(["a", "b"])
    assert journal.results == {}, "an inconclusive trial must not be recorded"


def _oracle_env_spy(replies):
    """Stand in for the oracle process, recording the env each trial was given.

    Monkeypatched rather than written as a shell script because the trap being
    tested is about *which server answered*, which has nothing to do with a
    shell -- and the script-based tests below cannot run where /bin/sh is not.
    """
    seen = []

    def fake_run(cmd, **kw):
        env = kw["env"]
        seen.append(env)
        return types.SimpleNamespace(stdout=replies(env), stderr="")

    fake_run.seen = seen
    return fake_run


def test_each_trial_is_issued_its_own_identity_to_serve(monkeypatch):
    """Guard 2 needs something to compare against, and a constant cannot be it."""
    spy = _oracle_env_spy(
        lambda env: f"VERDICT: PASS\nQUERIES: 4\nIDENTITY: {env['PN_TRIAL_IDENTITY']}\n"
    )
    monkeypatch.setattr(ib.subprocess, "run", spy)
    test = ib.make_test("unused", ib.Journal(None), log=lambda *a: None)

    assert test(["a"]) == ib.PASS
    assert test(["b"]) == ib.PASS

    first, second = spy.seen
    assert first["PN_TRIAL_UUID"] != second["PN_TRIAL_UUID"]
    assert first["PN_TRIAL_IDENTITY"] != second["PN_TRIAL_IDENTITY"], (
        "a reused identity cannot tell a stale server from the intended one"
    )
    assert first["PN_TRIAL_IDENTITY"].endswith(first["PN_TRIAL_UUID"].rsplit(":", 1)[-1])


def test_a_stale_server_aborts_instead_of_voting(monkeypatch):
    """An oracle answered by last trial's process reports last trial's identity."""
    spy = _oracle_env_spy(
        lambda env: "VERDICT: PASS\nQUERIES: 9\nIDENTITY: Bisect a-previous-trial\n"
    )
    monkeypatch.setattr(ib.subprocess, "run", spy)
    journal = ib.Journal(None)
    test = ib.make_test("unused", journal, retries=1, log=lambda *a: None)

    with pytest.raises(ib.Indeterminate):
        test(["a", "b"])
    assert journal.results == {}, "a mismatched identity must not be recorded"


def test_ddmin_propagates_the_abort():
    def test(subset):
        if len(subset) < 4:
            raise ib.Indeterminate("inconclusive")
        return ib.FAIL

    with pytest.raises(ib.Indeterminate):
        ib.ddmin([f"a{i}" for i in range(8)], test)


# --- trials are expensive, so they are memoised and journalled ---------------
def test_the_journal_survives_a_restart(tmp_path):
    path = tmp_path / "j.json"
    first = ib.Journal(str(path))
    first.put(["b", "a"], ib.PASS)

    second = ib.Journal(str(path))
    assert second.get(["a", "b"]) == ib.PASS, "order must not change the key"
    assert second.get(["a"]) is None


def test_a_cached_trial_does_not_rerun(tmp_path):
    path = tmp_path / "j.json"
    journal = ib.Journal(str(path))
    journal.put(["a"], ib.FAIL)
    # An oracle that would raise if consulted.
    test = ib.make_test("exit 1", journal, retries=0, log=lambda *a: None)
    assert test(["a"]) == ib.FAIL
    assert json.loads(path.read_text())["results"]


def test_repeated_subsets_are_asked_once():
    names = [f"a{i}" for i in range(24)]
    inner = oracle_for({"a4", "a17"})
    journal = ib.Journal(None)

    def counting(subset):
        cached = journal.get(subset)
        if cached is not None:
            return cached
        verdict = inner(subset)
        journal.put(subset, verdict)
        return verdict

    ib.ddmin(names, counting)
    keys = [tuple(sorted(c)) for c in inner.calls]
    assert len(keys) == len(set(keys)), "the same subset was printed twice"


# --- candidate selection -----------------------------------------------------
def test_identity_attributes_are_never_imported():
    donor = _ipp(["printer-uuid", "printer-name", "media-supported", "sides-supported"])
    got = ib.candidate_attributes(donor)
    assert got == ["media-supported", "sides-supported"]


def test_self_referential_uris_are_never_imported():
    """Importing these sends the client back to the real device mid-experiment."""
    donor = _ipp(["printer-uri-supported", "printer-more-info", "printer-icons",
                  "print-color-mode-supported"])
    assert ib.candidate_attributes(donor) == ["print-color-mode-supported"]


def test_only_printer_group_attributes_count():
    """Operation attributes describe the response, not the device."""
    body = _ipp(["media-supported"], group=0x01)
    assert ib.printer_attribute_names(body) == []


def test_continuation_values_do_not_duplicate_a_name():
    """A 1setOf arrives as one named value then zero-length-named extras."""
    out = struct.pack(">BBHI", 2, 0, 0, 1) + bytes([0x04])
    nb = b"media-supported"
    out += struct.pack(">BH", 0x44, len(nb)) + nb + struct.pack(">H", 2) + b"a4"
    out += struct.pack(">BHH", 0x44, 0, 2) + b"a5"  # continuation
    out += bytes([0x03])
    assert ib.printer_attribute_names(out) == ["media-supported"]


def test_a_truncated_capture_does_not_crash():
    assert ib.printer_attribute_names(b"") == []
    assert ib.printer_attribute_names(b"\x02\x00\x00\x00\x00\x00\x00\x01\x04\x44\xff") == []


def test_candidate_order_is_stable():
    """The search must be reproducible across runs to be resumable."""
    donor = _ipp(["b-attr", "a-attr", "c-attr"])
    assert ib.candidate_attributes(donor) == ["b-attr", "a-attr", "c-attr"]
    assert ib.candidate_attributes(donor) == ib.candidate_attributes(donor)


# --- the CLI, end to end -----------------------------------------------------
def _rig(tmp_path, oracle_body):
    donor = _ipp(["printer-uuid", "printer-make-and-model"] + [f"cap-{i:02d}" for i in range(20)])
    (tmp_path / "good.ipp").write_bytes(donor)
    (tmp_path / "brother.ipp").write_bytes(_ipp([f"cap-{i:02d}" for i in range(20)]))
    oracle = tmp_path / "oracle.sh"
    oracle.write_text(oracle_body)
    oracle.chmod(0o755)
    return [str(tmp_path / "brother.ipp"), str(tmp_path / "good.ipp"), "--oracle", str(oracle)]


def test_cli_finds_an_interacting_pair(tmp_path, capsys):
    argv = _rig(tmp_path, """#!/bin/sh
s=$(cat)
echo "QUERIES: 8"
echo "IDENTITY: $PN_TRIAL_IDENTITY"
if echo "$s" | grep -qx 'cap-03' && echo "$s" | grep -qx 'cap-17'; then
  echo "VERDICT: PASS"
else
  echo "VERDICT: FAIL"
fi
""")
    assert ib.main(argv + ["--journal", str(tmp_path / "j.json")]) == 0
    out = capsys.readouterr().out
    assert "cap-03" in out and "cap-17" in out
    assert "20 candidate attributes" in out, "identity attributes must not be candidates"


def test_cli_resumes_without_reprinting(tmp_path, capsys):
    body = """#!/bin/sh
s=$(cat)
echo "QUERIES: 8"
echo "IDENTITY: $PN_TRIAL_IDENTITY"
if echo "$s" | grep -qx 'cap-03' && echo "$s" | grep -qx 'cap-17'; then
  echo "VERDICT: PASS"
else
  echo "VERDICT: FAIL"
fi
"""
    argv = _rig(tmp_path, body) + ["--journal", str(tmp_path / "j.json")]
    assert ib.main(argv) == 0
    capsys.readouterr()

    # Replace the oracle with one that can only ever be inconclusive. A resumed
    # run must not consult it -- every trial is already known.
    (tmp_path / "oracle.sh").write_text("#!/bin/sh\ncat >/dev/null\necho nothing\n")
    (tmp_path / "oracle.sh").chmod(0o755)
    assert ib.main(argv) == 0
    out = capsys.readouterr().out
    assert "cap-03" in out and "cap-17" in out
    assert "trial " not in out, "a resumed run reprinted something"


def test_cli_aborts_when_the_premise_does_not_hold(tmp_path):
    """Importing everything must print, or the rig is not what was recorded."""
    ok = 'cat >/dev/null\necho "QUERIES: 8"\necho "IDENTITY: $PN_TRIAL_IDENTITY"\n'
    argv = _rig(tmp_path, f'#!/bin/sh\n{ok}echo "VERDICT: FAIL"\n')
    assert ib.main(argv) == 2

    argv = _rig(tmp_path, f'#!/bin/sh\n{ok}echo "VERDICT: PASS"\n')
    assert ib.main(argv) == 2


def test_cli_refuses_rather_than_tracebacks_on_an_inconclusive_premise(tmp_path):
    """A misconfigured rig is the likeliest way to start a multi-hour run.

    This regressed once: the sanity checks are trials too, and they sat outside
    the handler, so an inconclusive one crashed with a traceback instead of the
    stated refusal. It matters because a traceback reads like a bug in the tool
    rather than the thing it is -- the rig telling you Windows never asked it
    anything.
    """
    argv = _rig(tmp_path, '#!/bin/sh\ncat >/dev/null\necho "QUERIES: 0"\n'
                          'echo "IDENTITY: $PN_TRIAL_IDENTITY"\necho "VERDICT: PASS"\n')
    assert ib.main(argv + ["--retries", "0"]) == 3


# --- partitioning ------------------------------------------------------------
def test_partition_covers_everything_without_overlap():
    items = [f"a{i}" for i in range(17)]
    for n in range(1, 18):
        chunks = ib._partition(items, n)
        flat = [x for c in chunks for x in c]
        assert sorted(flat) == sorted(items)
        assert all(chunks), "no empty chunk"


def test_partition_never_exceeds_the_item_count():
    assert len(ib._partition(["a", "b"], 8)) == 2
