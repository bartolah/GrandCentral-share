#!/bin/bash
# auto_pull.sh — keep gc-host's checkout in sync with GitHub.
# Runs every 5 min via launchd (com.example.grandcentral.autopull).
#
# Fast-forwards main to origin/main when it's safe; otherwise logs and
# waits for a human/session to reconcile. Never merges, never rebases,
# never touches uncommitted work.

REPO=/Users/you/Code/GrandCentral
LOG="$REPO/logs/autopull.log"

log() { echo "$(date '+%Y-%m-%dT%H:%M:%S') $*" >> "$LOG"; }

cd "$REPO" || exit 1

if ! git fetch origin --quiet 2>>"$LOG"; then
    log "fetch failed (network or auth) — will retry next interval"
    exit 0
fi

LOCAL=$(git rev-parse main)
REMOTE=$(git rev-parse origin/main)

# Nothing new on GitHub (local == remote, or local is strictly ahead).
[ "$LOCAL" = "$REMOTE" ] && exit 0
if git merge-base --is-ancestor origin/main main; then
    exit 0
fi

# Remote has new commits. Refuse to touch a checkout with modified
# tracked files or a diverged history — a session on the mini owns those.
if git status --porcelain | grep -qv '^??'; then
    log "skip: origin/main is ahead but tracked files are modified locally"
    exit 0
fi
if ! git merge-base --is-ancestor main origin/main; then
    log "skip: histories diverged (local and GitHub both have new commits) — reconcile in a session"
    exit 0
fi

if git merge --ff-only origin/main --quiet 2>>"$LOG"; then
    log "pulled: $(git log --oneline "$LOCAL..$REMOTE" | wc -l | tr -d ' ') commit(s), now at $(git rev-parse --short main)"
    launchctl kickstart -k "gui/$(id -u)/com.example.grandcentral.dashboard" \
        && log "dashboard restarted" \
        || log "dashboard restart FAILED — check manually"
else
    log "ff merge failed unexpectedly — leaving checkout untouched"
fi
