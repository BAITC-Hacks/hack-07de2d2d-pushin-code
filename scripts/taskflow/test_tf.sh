#!/usr/bin/env bash

# Self-contained smoke/integration tests for the taskflow driver.  The fixture
# is a real Git repository with a bare origin; forge and package-manager CLIs
# are deliberately stubbed so this test is safe to run offline.

set -euo pipefail

TEST_SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
SOURCE_ROOT=$(cd "$TEST_SCRIPT_DIR/../.." && pwd -P)
TEST_TMP=$(mktemp -d "${TMPDIR:-/tmp}/taskflow-test.XXXXXX")
taskflow_test_cleanup() {
  [ "${TASKFLOW_KEEP_TEST_TMP:-}" = 1 ] || rm -rf -- "$TEST_TMP"
}
trap taskflow_test_cleanup EXIT

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

assert_contains() {
  local needle=$1 haystack=$2
  printf '%s\n' "$haystack" | grep -F -- "$needle" >/dev/null || fail "missing '$needle'"
}

assert_file() {
  [ -f "$1" ] || fail "expected file: $1"
}

ORIGIN="$TEST_TMP/origin.git"
REPO="$TEST_TMP/project"
WORKTREE="$TEST_TMP/task-worktree"
FAKEBIN="$TEST_TMP/bin"
mkdir -p "$FAKEBIN" "$REPO/scripts"

git init --bare -q "$ORIGIN"
git init --initial-branch=main -q "$REPO"
git -C "$REPO" config user.email taskflow@example.test
git -C "$REPO" config user.name Taskflow
cp -R "$SOURCE_ROOT/scripts/taskflow" "$REPO/scripts/"

mkdir -p "$REPO/tests"
cat > "$REPO/pyproject.toml" <<'EOF'
[project]
name = "taskflow-fixture"
version = "0.0.0"

[tool.pytest.ini_options]
pythonpath = ["."]
EOF
cat > "$REPO/app.py" <<'EOF'
def answer():
    return 42
EOF
cat > "$REPO/tests/test_app.py" <<'EOF'
import unittest

from app import answer


class AppTest(unittest.TestCase):
    def test_answer(self):
        self.assertEqual(answer(), 42)


if __name__ == "__main__":
    unittest.main()
EOF
cat > "$REPO/package.json" <<'EOF'
{
  "name": "taskflow-fixture",
  "scripts": {"lint": "lint", "test": "test", "build": "build"}
}
EOF
printf '{}\n' > "$REPO/package-lock.json"

git -C "$REPO" add -A
git -C "$REPO" commit -qm 'Initial fixture'
git -C "$REPO" remote add origin "$ORIGIN"
git -C "$REPO" push -q -u origin main
git -C "$ORIGIN" symbolic-ref HEAD refs/heads/main
git -C "$REPO" fetch -q origin
git -C "$REPO" remote set-head origin -a >/dev/null

cat > "$FAKEBIN/ruff" <<'EOF'
#!/usr/bin/env bash
printf 'ruff %s\n' "$*" >> "${TASKFLOW_LOG:?}"
exit 0
EOF
cat > "$FAKEBIN/npm" <<'EOF'
#!/usr/bin/env bash
printf 'npm %s\n' "$*" >> "${TASKFLOW_LOG:?}"
exit 0
EOF
cat > "$FAKEBIN/gh" <<'EOF'
#!/usr/bin/env bash
set -e
state=${TASKFLOW_GH_STATE:?}
body=${TASKFLOW_GH_BODY:?}
if [ "${1:-}" = auth ] && [ "${2:-}" = status ]; then exit 0; fi
if [ "${1:-}" = pr ] && [ "${2:-}" = list ]; then
  if [ -f "$state" ]; then
    printf '[{"number":7,"url":"https://github.example.test/org/project/pull/7"}]\n'
  else
    printf '[]\n'
  fi
  exit 0
fi
if [ "${1:-}" = pr ] && [ "${2:-}" = create ]; then
  while [ $# -gt 0 ]; do
    if [ "$1" = --body-file ]; then cp "$2" "$body"; shift 2; else shift; fi
  done
  : > "$state"
  printf 'https://github.example.test/org/project/pull/7\n'
  exit 0
fi
if [ "${1:-}" = pr ] && [ "${2:-}" = edit ]; then
  while [ $# -gt 0 ]; do
    if [ "$1" = --body-file ]; then cp "$2" "$body"; shift 2; else shift; fi
  done
  exit 0
fi
if [ "${1:-}" = pr ] && [ "${2:-}" = view ]; then
  printf '{"url":"https://github.example.test/org/project/pull/7"}\n'
  exit 0
fi
printf 'unexpected gh invocation: %s\n' "$*" >&2
exit 1
EOF
cat > "$FAKEBIN/open" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$1" >> "${TASKFLOW_BROWSER_LOG:?}"
EOF
chmod +x "$FAKEBIN/ruff" "$FAKEBIN/npm" "$FAKEBIN/gh" "$FAKEBIN/open"

export PATH="$FAKEBIN:$PATH"
export TASKFLOW_LOG="$TEST_TMP/commands.log"
export TASKFLOW_GH_STATE="$TEST_TMP/gh.state"
export TASKFLOW_GH_BODY="$TEST_TMP/gh.body"
export TASKFLOW_BROWSER_LOG="$TEST_TMP/browser.log"
export TASKFLOW_FORGE=github
export TASKFLOW_DRIVER_TEST_ACTIVE=1

start_output=$("$REPO/scripts/taskflow/tf.sh" start 'Add fixture endpoint' --name endpoint --base main --path "$WORKTREE")
assert_contains 'status=created' "$start_output"
[ "$(git -C "$WORKTREE" branch --show-current)" = endpoint ] || fail 'wrong task branch'

resume_output=$("$REPO/scripts/taskflow/tf.sh" start 'Add fixture endpoint' --name endpoint --base main --path "$WORKTREE")
assert_contains 'status=existing' "$resume_output"

printf '\n# task change\n' >> "$WORKTREE/app.py"
(cd "$WORKTREE" && git add app.py)
commit_output=$(cd "$WORKTREE" && scripts/taskflow/tf.sh commit --message 'Add fixture endpoint')
assert_contains 'status=committed' "$commit_output"

verify_output=$(cd "$WORKTREE" && scripts/taskflow/tf.sh verify)
assert_contains 'status=passed' "$verify_output"
receipt=$(printf '%s\n' "$verify_output" | sed -n 's/.* receipt=//p')
assert_file "$receipt"
assert_contains 'Ruff check' "$(cat "$receipt")"
assert_contains 'JavaScript frozen dependency install' "$(cat "$receipt")"
assert_contains 'JavaScript clean build' "$(cat "$receipt")"
assert_contains 'npm ci' "$(cat "$TASKFLOW_LOG")"

BODY="$TEST_TMP/body.md"
printf '%s\n' 'Implement the fixture endpoint.' > "$BODY"
ship_output=$(cd "$WORKTREE" && scripts/taskflow/tf.sh ship --title 'Fixture endpoint' --body-file "$BODY" --base main)
assert_contains 'status=created' "$ship_output"
assert_contains 'https://github.example.test/org/project/pull/7' "$ship_output"
assert_contains 'Branch: endpoint' "$(cat "$TASKFLOW_GH_BODY")"
assert_contains 'Exact taskflow verification receipt' "$(cat "$TASKFLOW_GH_BODY")"

printf '\n# unverified follow-up\n' >> "$WORKTREE/app.py"
(cd "$WORKTREE" && git add app.py && git commit -qm 'Unverified follow-up')
if (cd "$WORKTREE" && scripts/taskflow/tf.sh ship --title 'Should be blocked' --body-file "$BODY" --base main); then
  fail 'ship accepted a commit without a matching verification receipt'
fi
verify_output=$(cd "$WORKTREE" && scripts/taskflow/tf.sh verify)
assert_contains 'status=passed' "$verify_output"

update_output=$(cd "$WORKTREE" && scripts/taskflow/tf.sh ship --title 'Fixture endpoint updated' --body-file "$BODY" --base main)
assert_contains 'status=updated' "$update_output"
assert_file "$TASKFLOW_BROWSER_LOG"

STACKED="$TEST_TMP/stacked-worktree"
stacked_start=$("$REPO/scripts/taskflow/tf.sh" start 'Stacked fixture endpoint' --name stacked --base endpoint --path "$STACKED")
assert_contains 'status=created' "$stacked_start"
printf '\n# stacked task change\n' >> "$STACKED/app.py"
(cd "$STACKED" && git add app.py)
(cd "$STACKED" && scripts/taskflow/tf.sh commit --message 'Add stacked fixture endpoint') >/dev/null
stacked_verify=$(cd "$STACKED" && scripts/taskflow/tf.sh verify)
assert_contains 'status=passed' "$stacked_verify"
stacked_receipt=$(printf '%s\n' "$stacked_verify" | sed -n 's/.* receipt=//p')
assert_contains 'base=endpoint' "$(cat "$stacked_receipt")"
stacked_ship=$(cd "$STACKED" && scripts/taskflow/tf.sh ship --title 'Stacked fixture endpoint' --body-file "$BODY" --base endpoint)
assert_contains 'status=updated' "$stacked_ship"

printf 'PASS: taskflow driver integration tests\n'
