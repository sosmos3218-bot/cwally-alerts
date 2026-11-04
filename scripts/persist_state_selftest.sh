#!/usr/bin/env bash
# Local selftest for scripts/persist_state.sh (temp bare repo, no network, no ntfy).
# usage: bash scripts/persist_state_selftest.sh
set -u
S="$(cd "$(dirname "$0")" && pwd)/persist_state.sh"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
fail=0
check() { if [ "$2" = "$3" ]; then echo "PASS $1"; else echo "FAIL $1: got '$2' want '$3'"; fail=1; fi; }
cd "$T"
git init -q --bare -b main remote.git
git clone -q remote.git up 2>/dev/null; cd up; git config user.email t@t; git config user.name t
mkdir -p state scripts; echo '{"v":0}' > state/a.json; echo '{"items":[]}' > state/archive.json; echo x > README
printf 'print("archive regen (stub)")\n' > scripts/ntfy_archive.py
git add .; git commit -qm init; git push -q origin HEAD:main; cd ..
git clone -q remote.git run; cd up
echo '{"v":"upstream"}' > state/a.json; echo '{"items":[1]}' > state/archive.json; echo up > state/c.json
git add .; git commit -qm up; git push -q origin HEAD:main; cd ../run
echo '{"v":"run"}' > state/a.json; echo '{"items":[2]}' > state/archive.json; echo new > state/b.json; echo dirty > README
cat > ../remote.git/hooks/pre-receive <<HOOK
#!/bin/sh
n=\$(cat "$T/count" 2>/dev/null || echo 0); n=\$((n+1)); echo \$n > "$T/count"
[ \$n -le 2 ] && { echo "simulated 500"; exit 1; }
exit 0
HOOK
chmod +x ../remote.git/hooks/pre-receive
out=$(GITHUB_OUTPUT="$T/o1" PERSIST_DELAYS="0 0 0 0" bash "$S" "chore: t" state 2>&1); rc=$?
check "retry succeeds after 2 push failures" "$rc" 0
check "ok on try 3" "$(echo "$out" | grep -c 'persist ok (try 3/5)')" 1
check "state conflict keeps run state" "$(git show origin/main:state/a.json)" '{"v":"run"}'
check "archive conflict keeps upstream then regenerates" "$(git show origin/main:state/archive.json)" '{"items":[1]}'
check "upstream-only state kept" "$(git show origin/main:state/c.json)" up
check "new run state pushed" "$(git show origin/main:state/b.json)" new
check "non-state worktree untouched" "$(cat README)" dirty
printf '#!/bin/sh\nexit 1\n' > ../remote.git/hooks/pre-receive
echo '{"v":"run2"}' > state/a.json
GITHUB_OUTPUT="$T/o2" PERSIST_DELAYS="0 0 0 0" bash "$S" "chore: t2" state >/dev/null 2>&1; rc=$?
check "gives up after 5 tries with exit 1" "$rc" 1
check "persist_failed output set" "$(cat "$T/o2")" persist_failed=1
check "local state kept for artifact" "$(cat state/a.json)" '{"v":"run2"}'
[ "$fail" = 0 ] && echo "PERSIST SELFTEST PASS" || echo "PERSIST SELFTEST FAIL"
exit "$fail"
