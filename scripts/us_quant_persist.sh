#!/usr/bin/env bash
# Commit screeners/output/us onto the remote branch.
# usage: bash scripts/us_quant_persist.sh "<commit message>"
# - unshallow when needed, then git pull --rebase --autostash, then push
# - up to PERSIST_TRIES (5) rounds; backoff PERSIST_DELAYS (5 15 30 60 seconds)
# - before the commit, refresh diff against the newest earlier trade folder
# - a conflict under screeners/output/us keeps this run's files (--theirs)
# - a conflict anywhere else aborts that round
# - no force-push
set -u
msg=${1:?commit message}
case "$msg" in
  *"[skip ci]"*|*"[ci skip]"*|*"[skip actions]"*|*"[actions skip]"*) ;;
  *) msg="$msg [skip ci]" ;;
esac
tries=${PERSIST_TRIES:-5}
read -r -a delays <<<"${PERSIST_DELAYS:-5 15 30 60}"
remote=${PERSIST_REMOTE:-origin}
branch=${PERSIST_BRANCH:-main}

git config user.name "cwally-alerts-bot"
git config user.email "github-actions[bot]@users.noreply.github.com"

in_rebase() { [ -d "$(git rev-parse --git-path rebase-merge)" ] || [ -d "$(git rev-parse --git-path rebase-apply)" ]; }

unshallow() {
  if [ -f "$(git rev-parse --git-path shallow)" ]; then
    git fetch --unshallow "$remote" || git fetch --depth=400 "$remote" "$branch"
  fi
}

# During rebase, --theirs is the commit being replayed (this run).
# During a stash pop, --theirs is the stash (this run).
keep_our_outputs() {
  local bad=0 f
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    case "$f" in
      screeners/output/us/*)
        echo "persist: conflict $f -> keep this run's outputs"
        if git checkout --theirs -- "$f" 2>/dev/null; then
          git add -- "$f"
        else
          bad=1
        fi
        ;;
      *)
        echo "persist: conflict outside screeners/output/us ($f); abort this round"
        bad=1
        ;;
    esac
  done < <(git diff --name-only --diff-filter=U)
  [ "$bad" -eq 0 ]
}

finish_rebase() {
  local guard=0
  while in_rebase; do
    guard=$((guard + 1))
    if [ "$guard" -gt 50 ]; then
      git rebase --abort
      return 1
    fi
    if ! keep_our_outputs; then
      git rebase --abort
      return 1
    fi
    if git diff --cached --quiet; then
      GIT_EDITOR=true git rebase --skip >/dev/null 2>&1 || true
    else
      GIT_EDITOR=true git rebase --continue >/dev/null 2>&1 || true
    fi
  done
  if [ -n "$(git diff --name-only --diff-filter=U)" ]; then
    keep_our_outputs || return 1
  fi
  return 0
}

pull_rebase() {
  unshallow
  # fetch + rebase works on a detached HEAD (checkout default). git pull does not,
  # and a single git push then dies with "fetch first" when alert state lands first.
  git fetch "$remote" "$branch" || return 1
  if git rebase --autostash "$remote/$branch"; then
    finish_rebase
    return $?
  fi
  if in_rebase || [ -n "$(git diff --name-only --diff-filter=U)" ]; then
    finish_rebase
    return $?
  fi
  return 1
}

refresh_outputs() {
  python -m screeners.us_quant refresh-diff || echo "persist: diff refresh failed"
}

if pull_rebase; then
  refresh_outputs
else
  echo "persist: rebase before commit failed; committing the local outputs"
  in_rebase && git rebase --abort
fi

git add screeners/output/us
if git diff --staged --quiet; then
  echo unchanged
  exit 0
fi
git commit -q -m "$msg"

for ((i = 1; i <= tries; i++)); do
  if pull_rebase; then
    refresh_outputs
    if ! git diff --quiet -- screeners/output/us; then
      git add screeners/output/us
      git commit -q --amend --no-edit
    fi
    if git push "$remote" "HEAD:$branch"; then
      echo "persist ok (try $i/$tries)"
      exit 0
    fi
  fi
  in_rebase && git rebase --abort
  if [ "$i" -lt "$tries" ]; then
    d=${delays[$((i - 1))]:-${delays[-1]}}
    echo "persist: try $i/$tries failed; retry in ${d}s"
    sleep "$d"
  fi
done
echo "::error::US quant output commit failed after $tries tries"
exit 1
