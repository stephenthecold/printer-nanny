"""Search for the minimal set of advertised attributes that lets Windows render.

WHY THIS IS NOT A BISECT, AND WHY THAT MATTERS
----------------------------------------------
``deploy/WINDOWS-MSI-TESTING.md`` records the state of play: replaying the
Brother's captured attributes reproduces the failure 3/3, importing all 78
non-identity attributes from a working device makes it print, and **neither half
of those 78 is sufficient on its own**.

That last sentence is not a detail, it is the whole problem. "Split the set, test
each half, recurse into whichever half reproduces it" is binary search, and when
*both* halves come back negative binary search has nowhere to go -- it halts with
no answer. Both halves coming back negative is not bad luck here; it is the
defining signature of an **interaction**, where the effect needs two or more
attributes present together and neither is visible alone.

So the reason this search is unfinished may be partly that it was framed as a
bisect, and a bisect provably cannot converge on an interaction.

The algorithm that does is **delta debugging** (Zeller & Hildebrandt's ``ddmin``).
When every subset fails it does not give up: it tests the *complements*, and when
those fail too it increases granularity and tries again. That is exactly the
escape hatch a bisect lacks, and it is why ddmin returns a 1-minimal set -- one
where removing any single element loses the effect -- rather than nothing.

WHAT THIS SCRIPT DOES AND DOES NOT DO
-------------------------------------
It drives the search. It cannot render a print job, so it does not decide
anything: the verdict for each trial comes from an **oracle** you supply, a
command that runs on the real Windows box against the real device and reports
back. Everything above that seam -- which subsets to try, in what order, what the
answer means, and refusing to guess when a trial is inconclusive -- is here, and
is unit-tested.

THE THREE GUARDS ARE CODE, NOT DISCIPLINE
-----------------------------------------
The harness this drives has produced three confident wrong answers, each from a
guard that was a human's job to remember. They are enforced here instead:

1. **A fixed wait is a false-negative generator.** ``parse_verdict`` has three
   outcomes, not two. A trial that shows neither ``id=307`` nor a failing
   ``id=842`` is ``INDETERMINATE``, which is retried and then **aborts the
   search** -- it is never quietly folded into FAIL. That single coercion is what
   invalidated an entire earlier bisect.
2. **The server you configured must be the one serving.** Each trial is issued a
   fresh identity to serve as ``printer-make-and-model`` and must report the one
   it observed over IPP; a mismatch is INDETERMINATE, not a result. The identity
   has to be *per trial* for this to detect anything: the failure mode is the
   previous trial's server still holding the port, and a constant marker would
   answer for it just as convincingly as for the one you meant to start.
3. **The client must actually have queried.** A trial reporting zero
   ``Get-Printer-Attributes`` is a verdict about a device Windows had cached, so
   it is INDETERMINATE too. A fresh ``printer-uuid`` is minted per trial because
   Windows keys IPP devices on it.

Every trial is a physical print job, so results are memoised and journalled: an
interrupted search resumes without reprinting anything it already knows.

USAGE
-----
    python3 scripts/ipp_bisect.py brother.ipp good.ipp \\
        --oracle ./run-trial.sh --journal bisect.json

``run-trial.sh`` receives the subset on stdin (one attribute name per line), a
fresh UUID in ``$PN_TRIAL_UUID`` and the identity to serve in
``$PN_TRIAL_IDENTITY``; it must start the replay server with those attributes
imported, provision the queue, print, poll the event log, and print one
``VERDICT: PASS|FAIL|INDETERMINATE`` line to stdout along with ``QUERIES: <n>``
and ``IDENTITY: <string>``. See ``ORACLE_CONTRACT``.

``deploy/WINDOWS-IPP-BISECT-PROMPT.md`` is the operator's brief for the run:
the rig, the capture steps, and what the oracle has to do on the Windows side.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import struct
import subprocess
import sys
import uuid

PASS, FAIL, INDETERMINATE = "PASS", "FAIL", "INDETERMINATE"

PRINTER_ATTRS, END_ATTRS = 0x04, 0x03

ORACLE_CONTRACT = """\
The oracle command is run once per trial.

  stdin               the attribute names to import from the donor, one per line
                      (may be empty, meaning "import nothing")
  $PN_TRIAL_UUID      a fresh printer-uuid to serve, so Windows cannot answer
                      from its device cache
  $PN_TRIAL_IDENTITY  a fresh printer-make-and-model to serve, so a server left
                      over from the previous trial can be told apart from the
                      one you meant to start
  stdout              must contain, on lines of their own:
                          VERDICT: PASS | FAIL | INDETERMINATE
                          QUERIES: <number of Get-Printer-Attributes seen>
                          IDENTITY: <printer-make-and-model read back over IPP>
  exit code           ignored; the VERDICT line is what is read

Both markers have to survive the round trip or the trial does not get to vote:

  QUERIES: 0                      -> INDETERMINATE however it voted; that is a
                                     verdict about a device Windows had cached
  IDENTITY != $PN_TRIAL_IDENTITY  -> INDETERMINATE however it voted; some other
                                     server answered, so the verdict belongs to
                                     a configuration you did not set

Read IDENTITY back *from the server over IPP*, not from the value you passed on
the command line. Echoing $PN_TRIAL_IDENTITY unconditionally satisfies the check
and defeats it -- the point is to catch the case where the process you launched
died on "Address already in use" and the old one kept the port.
"""

# Never import these from the donor. Identity attributes make the fake claim to
# BE the donor, which changes what Windows caches and matches against rather
# than what it renders from; the self-referential URIs send the client back to
# the real device, at which point the experiment is measuring nothing. Both
# would produce results that look clean and mean nothing.
IDENTITY_ATTRS = frozenset({
    "printer-uuid",
    "printer-name",
    "printer-info",
    "printer-location",
    "printer-make-and-model",
    "printer-dns-sd-name",
    "printer-device-id",
    "printer-uri-supported",
    "uri-authentication-supported",
    "uri-security-supported",
    "printer-more-info",
    "printer-supply-info-uri",
    "printer-icons",
    "printer-strings-uri",
    "printer-privacy-policy-uri",
    "printer-charge-info-uri",
    "printer-geo-location",
    "printer-serial-number",
    "printer-firmware-string-version",
    "printer-config-change-time",
    "printer-up-time",
    "printer-current-time",
    "queued-job-count",
    "printer-state",
    "printer-state-reasons",
    "printer-state-message",
    "printer-is-accepting-jobs",
    "marker-levels",
    "marker-names",
    "marker-types",
    "marker-colors",
    "marker-low-levels",
    "marker-high-levels",
})


# --- reading captures --------------------------------------------------------
def printer_attribute_names(raw: bytes) -> "list[str]":
    """Names in the Printer-Attributes group of a captured IPP response, in order.

    Deliberately a standalone mini-parser rather than an import of
    ``ipp_replay.parse``: this must keep working if that script is edited, and
    the two disagreeing is something the tests can then catch.
    """
    if len(raw) < 8:
        return []
    names, i, in_printer = [], 8, False
    while i < len(raw):
        tag = raw[i]
        if tag <= 0x05:
            in_printer = tag == PRINTER_ATTRS
            i += 1
            if tag == END_ATTRS:
                break
            continue
        if i + 3 > len(raw):
            break
        nlen = struct.unpack(">H", raw[i + 1:i + 3])[0]
        name = raw[i + 3:i + 3 + nlen]
        j = i + 3 + nlen
        if j + 2 > len(raw):
            break
        vlen = struct.unpack(">H", raw[j:j + 2])[0]
        if in_printer and name:
            names.append(name.decode(errors="replace"))
        i = j + 2 + vlen
    return names


def candidate_attributes(donor_raw: bytes) -> "list[str]":
    """The attributes worth importing from the donor, in the donor's own order.

    Deliberately NOT filtered against the subject's attributes. It is tempting to
    skip the ones both devices already share, and it would shrink the search --
    but ``ipp_replay`` imports whole attributes, so a name the two have in common
    with *different values* is exactly the kind of thing that could be
    responsible. Filtering on the name would drop it and the search would
    confidently converge on the wrong answer, or on nothing.
    """
    donor = printer_attribute_names(donor_raw)
    seen, out = set(), []
    for name in donor:
        if name in IDENTITY_ATTRS or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


# --- the search --------------------------------------------------------------
class Indeterminate(Exception):
    """A trial could not be judged. The search stops rather than guessing."""


def _partition(items: "list[str]", n: int) -> "list[list[str]]":
    """Split into n near-equal chunks, largest first, no empties."""
    n = min(n, len(items))
    if n <= 0:
        return []
    q, r = divmod(len(items), n)
    out, i = [], 0
    for k in range(n):
        size = q + (1 if k < r else 0)
        out.append(items[i:i + size])
        i += size
    return [c for c in out if c]


def ddmin(candidates: "list[str]", test) -> "list[str]":
    """Smallest subset of `candidates` that still tests PASS (1-minimal).

    `test` takes a list of names and returns PASS or FAIL. It must raise rather
    than return anything else -- see the module docstring on why an inconclusive
    trial is not allowed to become a FAIL.

    Delta debugging, not bisection: when no single subset passes it tries the
    complements, and when those fail too it refines the partition instead of
    halting. That is what lets it find a set of attributes that only works
    together, which is the case the evidence says we are in.
    """
    c = list(candidates)
    n = 2
    while len(c) > 1:
        chunks = _partition(c, n)

        subset = next((ch for ch in chunks if test(ch) == PASS), None)
        if subset is not None:
            c, n = subset, 2
            continue

        complement = None
        for ch in chunks:
            rest = [x for x in c if x not in set(ch)]
            if rest and test(rest) == PASS:
                complement = rest
                break
        if complement is not None:
            c, n = complement, max(n - 1, 2)
            continue

        if n >= len(c):
            break
        n = min(2 * n, len(c))
    return c


def bisect_would_halt(candidates: "list[str]", test) -> bool:
    """True if a naive binary search gets stuck on the first split.

    Not used by the search -- it exists so the claim in the module docstring is
    demonstrable rather than asserted, and so a run can say plainly *why* the
    obvious approach was not the one taken.
    """
    halves = _partition(list(candidates), 2)
    return len(halves) == 2 and all(test(h) != PASS for h in halves)


# --- the oracle seam ---------------------------------------------------------
def parse_verdict(text: str, expect_identity: "str | None" = None) -> "tuple[str, int, str]":
    """-> (verdict, queries, identity) from an oracle's stdout.

    Three outcomes, never two. Anything unparseable is INDETERMINATE, because the
    alternative -- defaulting to FAIL -- is precisely the bug that invalidated the
    earlier bisect: a trial that merely took too long was recorded as evidence of
    absence.

    `expect_identity` is the marker this trial told the oracle to serve. Passing
    it turns guard 2 on; leaving it None parses without checking, which is only
    useful for inspecting an oracle's output by hand.
    """
    verdict, queries, identity = INDETERMINATE, -1, ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("VERDICT:"):
            got = line.split(":", 1)[1].strip().upper()
            verdict = got if got in (PASS, FAIL, INDETERMINATE) else INDETERMINATE
        elif line.startswith("QUERIES:"):
            raw = line.split(":", 1)[1].strip()
            queries = int(raw) if raw.lstrip("-").isdigit() else -1
        elif line.startswith("IDENTITY:"):
            identity = line.split(":", 1)[1].strip()
    # Guard 3, structurally: a run the client never queried is about a cached
    # device. It does not get to vote, whatever it claims.
    if queries <= 0:
        verdict = INDETERMINATE
    # Guard 2, structurally: a trial that read back some other server's identity
    # was answered by a configuration nobody chose -- classically the previous
    # trial's process, still holding the port after its replacement failed to
    # bind. A missing IDENTITY line fails this too; an oracle that does not
    # report the marker cannot show the check was made.
    if expect_identity is not None and identity != expect_identity:
        verdict = INDETERMINATE
    return verdict, queries, identity


class Journal:
    """Memoised trial results, persisted. Every miss is a physical print job."""

    def __init__(self, path: "str | None"):
        self.path = pathlib.Path(path) if path else None
        self.results: "dict[str, str]" = {}
        if self.path and self.path.exists():
            self.results = json.loads(self.path.read_text()).get("results", {})

    @staticmethod
    def key(subset: "list[str]") -> str:
        return "\n".join(sorted(subset))

    def get(self, subset: "list[str]") -> "str | None":
        return self.results.get(self.key(subset))

    def put(self, subset: "list[str]", verdict: str) -> None:
        self.results[self.key(subset)] = verdict
        if self.path:
            self.path.write_text(json.dumps({"results": self.results}, indent=2))


def make_test(oracle: str, journal: Journal, *, retries: int = 2, log=print):
    """Wrap the oracle command into the PASS/FAIL function ddmin expects."""

    def test(subset: "list[str]") -> str:
        cached = journal.get(subset)
        if cached is not None:
            log(f"  cached  {len(subset):3d} attrs -> {cached}")
            return cached
        for attempt in range(1, retries + 2):
            trial_uuid = f"urn:uuid:{uuid.uuid4()}"
            # Derived from the same mint so a log line ties the served identity
            # back to the UUID, and unique per trial so guard 2 can see a stale
            # server rather than merely assert one is not there.
            trial_identity = f"Bisect {trial_uuid.rsplit(':', 1)[-1]}"
            env = dict(os.environ, PN_TRIAL_UUID=trial_uuid,
                       PN_TRIAL_IDENTITY=trial_identity)
            proc = subprocess.run(
                oracle, shell=True, env=env, input="\n".join(subset),
                capture_output=True, text=True,
            )
            verdict, queries, identity = parse_verdict(
                proc.stdout, expect_identity=trial_identity)
            stale = "" if identity == trial_identity else f", wanted {trial_identity!r}"
            log(f"  trial   {len(subset):3d} attrs -> {verdict} "
                f"(queries={queries}, identity={identity!r}{stale}, attempt {attempt})")
            if verdict in (PASS, FAIL):
                journal.put(subset, verdict)
                return verdict
        raise Indeterminate(
            f"{retries + 1} trials of a {len(subset)}-attribute subset were "
            f"inconclusive; refusing to record a verdict. Last stderr:\n"
            f"{proc.stderr[-2000:]}"
        )

    return test


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # `subject`, `donor` and `--oracle` are all REQUIRED for a run, but they are
    # declared optional here and enforced below by hand. The reason is
    # `--contract`: argparse evaluates required-ness during parse_args, i.e.
    # *before* any early return we could write, so declaring them the obvious way
    # makes `--contract` exit 2 with a usage message instead of printing the
    # contract. That is exactly what happened -- the bare form is documented in
    # deploy/WINDOWS-IPP-BISECT-PROMPT.md, deploy/WINDOWS-MSI-TESTING.md and
    # deploy/HARDWARE-VERIFICATION.md, and none of them worked. `--contract` is
    # an informational flag like `--help`; an operator reaching for it is asking
    # what to build BEFORE they have captures or an oracle to name, so demanding
    # three placeholder arguments to read the spec is backwards.
    ap.add_argument("subject", nargs="?",
                    help="capture from the failing device (brother.ipp)")
    ap.add_argument("donor", nargs="?",
                    help="capture from a working device (good.ipp)")
    ap.add_argument("--oracle", help="command that runs one trial")
    ap.add_argument("--journal", default=None, help="resume/record file")
    ap.add_argument("--retries", type=int, default=2,
                    help="extra attempts before an inconclusive trial aborts")
    ap.add_argument("--contract", action="store_true", help="print the oracle contract and exit")
    args = ap.parse_args(argv)

    if args.contract:
        print(ORACLE_CONTRACT)
        return 0

    # Enforced here rather than by argparse, per the note above. `ap.error` is
    # used so the exit code (2) and the usage line stay identical to what
    # argparse would have produced -- an operator who omits an argument sees no
    # difference, and only `--contract` behaves better than it did.
    missing = [
        name for name, value in (
            ("subject", args.subject), ("donor", args.donor), ("--oracle", args.oracle)
        ) if not value
    ]
    if missing:
        ap.error("the following arguments are required: " + ", ".join(missing))

    donor_raw = pathlib.Path(args.donor).read_bytes()
    candidates = candidate_attributes(donor_raw)
    print(f"{len(candidates)} candidate attributes from the donor")

    journal = Journal(args.journal)
    test = make_test(args.oracle, journal, retries=args.retries)

    # The sanity checks are trials like any other, so they can be inconclusive
    # too -- and an inconclusive premise check is the single most likely way to
    # start a multi-hour run against a misconfigured rig. It gets the same
    # refusal as the search, not a traceback.
    try:
        print("sanity: the full set must PASS and the empty set must FAIL, "
              "or the search is measuring something else")
        if test(candidates) != PASS:
            print("ABORT: importing everything did not print. The donor, the oracle "
                  "or the device changed since that was established.", file=sys.stderr)
            return 2
        if test([]) != FAIL:
            print("ABORT: importing nothing printed. The subject capture no longer "
                  "reproduces the failure, so there is nothing to minimise.", file=sys.stderr)
            return 2

        minimal = ddmin(candidates, test)
    except Indeterminate as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        return 3

    print("\n1-minimal set -- removing any one of these loses the effect:")
    for name in minimal:
        print(f"  {name}")
    print(f"\n{len(minimal)} of {len(candidates)} attributes, "
          f"{len(journal.results)} trials recorded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
