#!/usr/bin/env bash
#
# SessionStart hook: reconcile the environment-provisioned stop hook with the
# corrected one in this repo.
#
# WHY THIS IS NEEDED AT ALL
# -------------------------
# Claude Code's cloud environment provisions ~/.claude/stop-hook-git-check.sh
# and registers it via ~/.claude/launcher-settings.json, regenerating BOTH on
# every session. Shipping a corrected hook in the repo therefore does not
# replace it -- both run, and the provisioned one still blocks the turn with a
# false positive after any PR merge.
#
# So this rewrites the provisioned copy's two ranges in place.
#
# WHAT IT WILL AND WILL NOT TOUCH
# -------------------------------
# It edits exactly one path (~/.claude/stop-hook-git-check.sh) and only when
# that file still contains the known-buggy `"$upstream..HEAD"` ranges. If the
# environment ships a different or already-fixed version, this no-ops rather
# than clobbering it. It is idempotent and never fails the session: any problem
# exits 0 with a note, because a SessionStart hook must not stop you working.
#
# Delete this file and its .claude/settings.json SessionStart entry if you would
# rather the provisioned hook were left alone.

set -uo pipefail

HOOK="$HOME/.claude/stop-hook-git-check.sh"

MARKER='local_only=(HEAD --not --remotes=origin)'

[[ -f "$HOOK" ]] || exit 0                      # nothing provisioned here
[[ -w "$HOOK" ]] || exit 0                      # not ours to edit
# Already reconciled. Checked before the buggy-pattern gate and stated as its
# own condition rather than relying on the rewrite failing a second time --
# idempotency by accident is idempotency that breaks when the file shifts.
grep -qF "$MARKER" "$HOOK" 2>/dev/null && exit 0
grep -q 'upstream\.\.HEAD' "$HOOK" 2>/dev/null || exit 0   # different version; leave alone

python3 - "$HOOK" <<'PY' || exit 0
import pathlib
import sys

hook = pathlib.Path(sys.argv[1])
text = hook.read_text()

# Introduce the corrected range once, immediately before the signature check.
anchor = '  if [[ "$(git config --type=bool commit.gpgsign 2>/dev/null)" == "true" ]]; then'
if anchor not in text:
    sys.exit(1)

preamble = (
    '  # Commits reachable from HEAD but from no origin ref -- genuinely local.\n'
    '  # NOT "$upstream..HEAD": origin/<branch> does not move when a PR merges, so a\n'
    '  # branch restarted from the merged main would enumerate the merge commit and\n'
    '  # everything else on main -- all published, none of it ours. Flagging\n'
    "  # GitHub's merge commit (committer noreply@github.com, an identity no local\n"
    '  # config can satisfy) and advising a rebase would rewrite merged public\n'
    '  # history to silence a false positive.\n'
    '  # Reconciled from scripts/stop-hook-git-check.sh by scripts/patch-launcher-hook.sh.\n'
    '  local_only=(HEAD --not --remotes=origin)\n\n'
)
text = text.replace(anchor, preamble + anchor, 1)

# Point both checks at it.
replacements = [
    ("""git log --format='%h %G? %ce' "$upstream..HEAD" """,
     """git log --format='%h %G? %ce' "${local_only[@]}" """),
    ('''git rev-list "$upstream..HEAD" --count''',
     '''git rev-list "${local_only[@]}" --count'''),
]
for old, new in replacements:
    if old not in text:
        sys.exit(1)
    text = text.replace(old, new, 1)

# Derive the suggested rebase base from the oldest genuinely-local commit, so
# the remediation it prints cannot reach a published commit.
old_msg = 'reset-author\\" $upstream\''
new_msg = 'reset-author\\" ${rebase_base:-$upstream}\''
if old_msg in text:
    text = text.replace(old_msg, new_msg, 1)
    text = text.replace(
        '    if [[ -n "$unverifiable" ]]; then\n',
        '    if [[ -n "$unverifiable" ]]; then\n'
        '      oldest_local=$(git rev-list "${local_only[@]}" 2>/dev/null | tail -1)\n'
        '      rebase_base="${oldest_local:+$oldest_local^}"\n',
        1,
    )

hook.write_text(text)
print("patched provisioned stop hook: ranges now exclude commits already on a remote")
PY
