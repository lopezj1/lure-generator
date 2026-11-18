#!/usr/bin/env bash
# Remove feature worktrees under .claude/worktrees/ whose PR has been merged.
#
# Usage: tools/cleanup_worktrees.sh [--dry-run]
#
# A worktree is removed only when its branch has a merged PR whose head commit equals the
# local branch tip and the worktree has no uncommitted changes. Anything else is kept and
# reported. Works with squash merges (checks GitHub, not `git branch --merged`). Safe to
# run from the main checkout or from inside any worktree.
set -euo pipefail

dry_run=false
[[ ${1:-} == "--dry-run" ]] && dry_run=true

here=$(pwd -P)
main_root=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")
cd "$main_root"
git fetch --prune --quiet origin
owner_repo=$(gh api 'repos/{owner}/{repo}' --jq .full_name)
owner=${owner_repo%%/*}

git worktree list --porcelain | awk '/^worktree /{wt=$2} /^branch /{print wt" "$2}' |
while read -r wt ref; do
    [[ $wt == "$main_root/.claude/worktrees/"* ]] || continue
    branch=${ref#refs/heads/}
    merged_sha=$(gh api "repos/$owner_repo/pulls?head=$owner:$branch&state=closed" \
        --jq 'map(select(.merged_at != null)) | .[0].head.sha // ""')
    if [[ -z $merged_sha ]]; then
        echo "keep    $branch (no merged PR)"
        continue
    fi
    if [[ $(git rev-parse "$branch") != "$merged_sha" ]]; then
        echo "keep    $branch (local commits not in the merged PR)"
        continue
    fi
    if [[ -n $(git -C "$wt" status --porcelain) ]]; then
        echo "keep    $branch (uncommitted changes in $wt)"
        continue
    fi
    if [[ $here == "$wt"* ]]; then
        echo "keep    $branch (current shell is inside it; rerun from elsewhere)"
        continue
    fi
    if $dry_run; then
        echo "would remove $wt and branch $branch"
    else
        git worktree remove "$wt"
        git branch -D "$branch" >/dev/null
        echo "removed $wt and branch $branch"
    fi
done
$dry_run || git worktree prune
