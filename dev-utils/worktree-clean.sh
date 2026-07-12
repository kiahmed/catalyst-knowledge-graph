#!/usr/bin/env bash
# Safely tear down a worktree everywhere: verify its branch is fully merged into
# origin/main, then remove (1) the worktree, (2) the local branch, (3) the
# remote branch. Refuses if the branch has commits not yet in origin/main, so
# you can't delete unmerged work by accident.
#
# Usage: make worktree-clean <name>          (name = dir under .claude/worktrees/)
#        make worktree-clean <name> FORCE=1  (skip the merged check)
set -uo pipefail

NAME="${1:-}"
FORCE="${2:-}"
[ -z "$NAME" ] && { echo "usage: make worktree-clean WT=<name> [FORCE=1]"; exit 2; }

# Always operate from the MAIN working tree, never from inside the target worktree.
MAIN="$(git worktree list --porcelain | awk '/^worktree /{print $2; exit}')"
cd "$MAIN" || { echo "can't locate main worktree"; exit 1; }

# A name with a slash (e.g. ship/20260712-0301) is a plain branch, not a
# worktree under .claude/worktrees/ — `make ship` cuts those directly.
case "$NAME" in
  */*) WT_DIR=""; BRANCH="$NAME" ;;
  *)   WT_DIR=".claude/worktrees/$NAME"; BRANCH="worktree-$NAME" ;;
esac
# If the worktree exists, trust the branch it actually has checked out.
if [ -n "$WT_DIR" ] && [ -d "$WT_DIR" ]; then
  b="$(git -C "$WT_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  [ -n "${b:-}" ] && [ "$b" != "HEAD" ] && BRANCH="$b"
fi
echo "[worktree-clean] dir=$WT_DIR branch=$BRANCH"

# --- Safety: branch must be fully merged into origin/main -------------------
git fetch origin --quiet || true
if [ "${FORCE#FORCE=}" = "1" ]; then
  echo "  FORCE set — skipping merged check"
elif ! git rev-parse --verify "$BRANCH" >/dev/null 2>&1; then
  echo "  local branch $BRANCH not found — will just clean up dir/remote"
elif git merge-base --is-ancestor "$BRANCH" origin/main; then
  echo "  ✓ $BRANCH is fully merged into origin/main"
else
  ahead="$(git rev-list --count origin/main.."$BRANCH" 2>/dev/null || echo '?')"
  echo "  ✗ REFUSING: $BRANCH has $ahead commit(s) not in origin/main."
  echo "    Merge its PR first (squash-merges look unmerged here — use FORCE=1 then)."
  exit 1
fi

# --- 1. worktree dir + registration -----------------------------------------
if [ -n "$WT_DIR" ] && [ -d "$WT_DIR" ]; then
  git worktree remove "$WT_DIR" && echo "  removed worktree $WT_DIR"
fi
git worktree prune

# --- 2. local branch --------------------------------------------------------
# `make ship` leaves you ON the ship branch — step off it first (a checked-out
# branch can't be deleted) and bring main up to date with the merged PR.
if [ "$(git rev-parse --abbrev-ref HEAD)" = "$BRANCH" ]; then
  git switch main && git pull --ff-only origin main
fi
if git rev-parse --verify "$BRANCH" >/dev/null 2>&1; then
  git branch -D "$BRANCH" && echo "  deleted local branch $BRANCH"
fi

# --- 3. remote branch -------------------------------------------------------
if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
  git push origin --delete "$BRANCH" && echo "  deleted remote branch $BRANCH"
else
  echo "  remote branch $BRANCH already gone"
fi
echo "[worktree-clean] done."
