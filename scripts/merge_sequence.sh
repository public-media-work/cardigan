#!/usr/bin/env bash
# Land several PRs one at a time under main's strict up-to-date rule.
#
# For each PR, in the order given: update the branch from main if it is
# behind, wait for CI on the new head, then squash-merge pinned to that
# exact head SHA. Each merge puts the next PR behind, so they can't be
# merged in parallel. Stops at the first PR that fails CI, conflicts,
# or times out. PRs already merged are left alone.
#
# Usage:
#   scripts/merge_sequence.sh 421 422 462
#   MERGE_SEQ_TIMEOUT_MIN=45 scripts/merge_sequence.sh 490
#
# It doesn't pass `--delete-branch`: that flag also deletes the local branch,
# and errors (while the merge still succeeds) when that branch is checked out
# in a worktree. The remote branch is deleted separately instead.
# The repo has no auto-merge, so this script is the merge.

set -euo pipefail

POLL_SEC=30
TIMEOUT_MIN="${MERGE_SEQ_TIMEOUT_MIN:-30}"

[ $# -gt 0 ] || { echo "usage: $0 <pr> [<pr> ...]" >&2; exit 2; }

state_of() { gh pr view "$1" --json mergeStateStatus -q .mergeStateStatus; }

failed_checks() {
    gh pr checks "$1" --json name,bucket \
        -q '[.[] | select(.bucket == "fail") | .name] | join(", ")' 2>/dev/null || true
}

for pr in "$@"; do
    if [ "$(gh pr view "$pr" --json state -q .state)" = "MERGED" ]; then
        echo "PR $pr: already merged, skipping"
        continue
    fi

    deadline=$(( $(date +%s) + TIMEOUT_MIN * 60 ))
    while :; do
        st="$(state_of "$pr")"
        case "$st" in
            CLEAN) break ;;
            BEHIND)
                echo "PR $pr: behind main, updating branch"
                gh pr update-branch "$pr" >/dev/null
                ;;
            DIRTY)
                echo "PR $pr: conflicts with main, stopping" >&2
                exit 1
                ;;
        esac
        f="$(failed_checks "$pr")"
        if [ -n "$f" ]; then
            echo "PR $pr: CI failed ($f), stopping" >&2
            exit 1
        fi
        if [ "$(date +%s)" -ge "$deadline" ]; then
            echo "PR $pr: still $st after ${TIMEOUT_MIN} min, stopping" >&2
            exit 1
        fi
        sleep "$POLL_SEC"
    done

    sha="$(gh pr view "$pr" --json headRefOid -q .headRefOid)"
    branch="$(gh pr view "$pr" --json headRefName -q .headRefName)"
    gh pr merge "$pr" --squash --match-head-commit "$sha"

    # The merge can take a few seconds to show; a retry in that window
    # answers "Merge already in progress", so wait instead of retrying.
    for _ in 1 2 3 4 5 6; do
        [ "$(gh pr view "$pr" --json state -q .state)" = "MERGED" ] && break
        sleep 10
    done
    if [ "$(gh pr view "$pr" --json state -q .state)" != "MERGED" ]; then
        echo "PR $pr: merge did not complete within 60 s, stopping" >&2
        exit 1
    fi
    git push origin --delete "$branch" >/dev/null 2>&1 \
        || echo "PR $pr: remote branch $branch not deleted (already gone?)"
    echo "PR $pr: merged at $sha"
done
