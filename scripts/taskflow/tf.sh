#!/usr/bin/env bash

# Deterministic Jira-free task -> worktree -> verification -> PR/MR workflow.

set -euo pipefail

TF_SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=lib.sh
source "$TF_SCRIPT_DIR/lib.sh"

tf_usage() {
  cat <<'EOF'
Usage:
  tf.sh doctor
  tf.sh start <plain task> [--name slug] [--base branch] [--path path]
  tf.sh list
  tf.sh commit --message text
  tf.sh verify [path]
  tf.sh ship --title text --body-file path [--base branch]

The driver never contacts Jira and never merges.  It creates/resumes isolated
Git worktrees, runs the repository's Python and React/JavaScript quality gates,
pushes to origin, and creates or updates a GitHub PR or GitLab MR.
EOF
}

tf_main() {
  local action=${1:-}
  case "$action" in
    doctor) shift; [ $# -eq 0 ] || { tf_error 'doctor accepts no arguments.'; return 1; }; tf_doctor ;;
    start) shift; tf_start "$@" ;;
    list) shift; [ $# -eq 0 ] || { tf_error 'list accepts no arguments.'; return 1; }; tf_list ;;
    commit) shift; tf_commit "$@" ;;
    verify) shift; [ $# -le 1 ] || { tf_error 'verify accepts at most one path.'; return 1; }; tf_verify "${1:-.}" ;;
    ship) shift; tf_ship "$@" ;;
    -h|--help|help) tf_usage ;;
    '') tf_usage; return 1 ;;
    *) tf_error "Unknown action: $action"; tf_usage >&2; return 1 ;;
  esac
}

tf_main "$@"
