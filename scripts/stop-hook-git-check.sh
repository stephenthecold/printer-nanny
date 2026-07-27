#!/usr/bin/env bash
#
# Stop hook: refuse to end a turn with work that would be lost or that GitHub
# will render as Unverified. Registered by .claude/settings.json.
#
# WHY THIS LIVES IN THE REPO
# -------------------------
# Claude Code's cloud environment provisions its own copy at
# ~/.claude/stop-hook-git-check.sh and regenerates it on every session, so an
# in-place fix there does not survive. This is the corrected, reviewable,
# versioned copy. scripts/patch-launcher-hook.sh reconciles the provisioned copy
# to match at SessionStart, since otherwise both run and the buggy one still
# blocks.
#
# THE BUG THIS FIXES
# ------------------
# The provisioned version scopes both checks to "$upstream..HEAD" -- commits not
# on *this branch's* remote ref -- when it means commits not on *any* remote.
#
# origin/<branch> does not move when a PR merges: GitHub merges the branch into
# main and leaves the branch ref at its pre-merge tip. So a branch restarted
# from the merged main -- the documented way to begin follow-up work -- makes
# that range enumerate the merge commit and everything else that landed on main.
# All published. None of it local.
#
# It then flagged GitHub's own merge commit, whose committer is
# noreply@github.com (an identity no local git config can ever satisfy), and
# advised `git rebase` on it -- which would rewrite merged public history to
# silence a false positive. That is a bad instruction to give an agent.
#
# The fix is `HEAD --not --remotes=origin`: reachable from HEAD, from no origin
# ref. Published commits are never flagged whichever branch carries them, while
# genuinely local work still is. It assumes remote-tracking refs are current; a
# stale fetch can reintroduce a false positive, and `git fetch` clears it.

set -uo pipefail

input=$(cat)

# Recursion guard.
if [[ "$(echo "$input" | jq -r '.stop_hook_active' 2>/dev/null)" == "true" ]]; then
  exit 0
fi

git rev-parse --git-dir >/dev/null 2>&1 || exit 0

# Every message below tells the user to push. Meaningless with no remote.
[[ -n "$(git remote)" ]] || exit 0

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "There are uncommitted changes in the repository. Please commit and push these changes to the remote branch." >&2
  exit 2
fi

if [[ -n "$(git ls-files --others --exclude-standard)" ]]; then
  echo "There are untracked files in the repository. Please commit and push these changes to the remote branch." >&2
  exit 2
fi

current_branch=$(git branch --show-current)
[[ -n "$current_branch" ]] || exit 0

if git rev-parse --verify --quiet "origin/$current_branch" >/dev/null 2>&1; then
  upstream="origin/$current_branch"
else
  upstream="origin/HEAD"
fi

# Commits reachable from HEAD but from no origin ref -- i.e. genuinely local.
# See the header for why this is not "$upstream..HEAD".
local_only=(HEAD --not --remotes=origin)

# Commits GitHub will show as Unverified: unsigned (%G? == N), or signed under a
# committer email other than the one the signing key is registered to.
#
# Gated on the checkout already being configured for that identity. This script
# is checked in, so it also runs for human collaborators using Claude Code --
# and a contributor committing as themselves is not subject to Anthropic's
# signing identity. Without this gate the hook would block their every turn.
configured_email=$(git config user.email 2>/dev/null || true)
signing_on=$(git config --type=bool commit.gpgsign 2>/dev/null || true)

if [[ "$signing_on" == "true" && "$configured_email" == "noreply@anthropic.com" ]]; then
  unverifiable=$(git log --format='%h %G? %ce' "${local_only[@]}" 2>/dev/null \
    | awk '$2 == "N" || $3 != "noreply@anthropic.com"')
  if [[ -n "$unverifiable" ]]; then
    # Rebase from the parent of the oldest genuinely-local commit. Safe by
    # construction: every commit in that range is unpublished, so the rewrite
    # cannot reach anything already on a remote.
    oldest_local=$(git rev-list "${local_only[@]}" 2>/dev/null | tail -1)
    rebase_base="${oldest_local:+$oldest_local^}"
    echo "There are commit(s) on branch '$current_branch' that GitHub will show as Unverified (missing signature, or committer email is not noreply@anthropic.com):" >&2
    echo "$unverifiable" >&2
    echo "Please run 'git config user.email noreply@anthropic.com && git config user.name Claude', then 'git commit --amend --no-edit --reset-author' for the tip commit, or 'git rebase --exec \"git commit --amend --no-edit --reset-author\" ${rebase_base:-$upstream}' for earlier commits, then push." >&2
    exit 2
  fi
fi

unpushed=$(git rev-list "${local_only[@]}" --count 2>/dev/null) || unpushed=0
if [[ "$unpushed" -gt 0 ]]; then
  if [[ "$upstream" == "origin/$current_branch" ]]; then
    echo "There are $unpushed unpushed commit(s) on branch '$current_branch'. Please push these changes to the remote repository." >&2
  else
    echo "Branch '$current_branch' has $unpushed unpushed commit(s) and no remote branch. Please push these changes to the remote repository." >&2
  fi
  exit 2
fi

exit 0
