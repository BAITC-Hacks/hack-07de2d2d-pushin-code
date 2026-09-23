#!/usr/bin/env bash

# Jira-free, forge-agnostic Git task workflow helpers.
#
# This file is sourced by tf.sh.  It intentionally keeps all state in Git's
# common directory, so a verification receipt follows every worktree without
# adding generated files to a user's checkout.

set -o pipefail

TF_SCRIPT_DIR=${TF_SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)}
TF_PROJECT_ROOT=${TF_PROJECT_ROOT:-$(cd "$TF_SCRIPT_DIR/../.." && pwd -P)}
TF_TASKFLOW_NAME=taskflow

tf_error() {
  printf '[taskflow] ERROR: %s\n' "$*" >&2
}

tf_warn() {
  printf '[taskflow] WARNING: %s\n' "$*" >&2
}

tf_info() {
  printf '[taskflow] %s\n' "$*"
}

tf_result() {
  printf 'RESULT %s\n' "$*"
}

tf_die() {
  tf_error "$*"
  return 1
}

tf_command_exists() {
  command -v "$1" >/dev/null 2>&1
}

tf_lower() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

tf_abs_dir() {
  local candidate=${1:-}
  [ -n "$candidate" ] || return 1
  [ -d "$candidate" ] || return 1
  (cd "$candidate" && pwd -P)
}

tf_abs_file() {
  local candidate=${1:-} parent name
  [ -f "$candidate" ] || return 1
  parent=$(cd "$(dirname "$candidate")" && pwd -P) || return 1
  name=$(basename "$candidate")
  printf '%s/%s\n' "$parent" "$name"
}

tf_git_root() {
  local path=${1:-.} root
  root=$(git -C "$path" rev-parse --show-toplevel 2>/dev/null) || return 1
  tf_abs_dir "$root"
}

tf_git_common_dir() {
  local path=${1:-.} common
  common=$(git -C "$path" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || \
    common=$(git -C "$path" rev-parse --git-common-dir 2>/dev/null) || return 1
  case "$common" in
    /*) tf_abs_dir "$common" ;;
    *) tf_abs_dir "$(tf_git_root "$path")/$common" ;;
  esac
}

tf_validate_project() {
  local expected actual common

  expected=$(tf_abs_dir "$TF_PROJECT_ROOT") || return 1
  [ -e "$expected/.git" ] || {
    tf_error "This project is not an independent Git repository: $expected has no .git."
    tf_error "Refusing to use an ancestor Git repository as the task repository."
    return 1
  }

  actual=$(tf_git_root "$expected") || {
    tf_error "Unable to resolve the Git root for $expected."
    return 1
  }
  if [ "$actual" != "$expected" ]; then
    tf_error "The taskflow project root is $expected, but Git resolves it to $actual."
    tf_error "Refusing to operate on an ancestor repository. Initialize or clone this project here."
    return 1
  fi
  common=$(tf_git_common_dir "$expected") || {
    tf_error "Unable to resolve the Git common directory for $expected."
    return 1
  }
  [ -n "$common" ] || return 1
  printf '%s\n' "$expected"
}

tf_validate_worktree() {
  local path=${1:-.} expected_root expected_common actual_root actual_common
  path=$(tf_abs_dir "$path") || {
    tf_error "Worktree path is not a directory: ${1:-}"
    return 1
  }
  expected_root=$(tf_validate_project) || return 1
  expected_common=$(tf_git_common_dir "$expected_root") || return 1
  actual_root=$(tf_git_root "$path") || {
    tf_error "Not a Git worktree: $path"
    return 1
  }
  actual_common=$(tf_git_common_dir "$path") || {
    tf_error "Unable to resolve the Git common directory for $path."
    return 1
  }
  if [ "$actual_common" != "$expected_common" ]; then
    tf_error "Worktree $path does not belong to this taskflow repository."
    return 1
  fi
  printf '%s\n' "$actual_root"
}

tf_remote_url() {
  git -C "$1" remote get-url origin 2>/dev/null
}

tf_remote_host() {
  local url=${1:-} host
  case "$url" in
    *@*)
      host=${url#*@}
      host=${host%%:*}
      host=${host%%/*}
      ;;
    *://*)
      host=${url#*://}
      host=${host%%/*}
      host=${host%%:*}
      ;;
    *)
      host=${url%%:*}
      host=${host%%/*}
      ;;
  esac
  tf_lower "$host"
}

tf_detect_forge() {
  local path=${1:-$TF_PROJECT_ROOT} url host override=${TASKFLOW_FORGE:-}
  if [ -n "$override" ]; then
    override=$(tf_lower "$override")
    case "$override" in
      github|gitlab) printf '%s\n' "$override"; return 0 ;;
      *) tf_error "TASKFLOW_FORGE must be github or gitlab, got '$override'."; return 1 ;;
    esac
  fi
  url=$(tf_remote_url "$path") || {
    tf_error "No origin remote is configured for $path."
    return 1
  }
  host=$(tf_remote_host "$url")
  case "$host" in
    github.com|*.github.com) printf 'github\n' ;;
    gitlab.com|*.gitlab.com) printf 'gitlab\n' ;;
    *)
      tf_error "Cannot identify GitHub or GitLab from origin host '$host'."
      tf_error "Set TASKFLOW_FORGE=github or TASKFLOW_FORGE=gitlab for a custom host."
      return 1
      ;;
  esac
}

tf_default_base() {
  local path=${1:-$TF_PROJECT_ROOT} remote_head branch candidate
  remote_head=$(git -C "$path" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || true)
  if [ -n "$remote_head" ]; then
    printf '%s\n' "${remote_head#origin/}"
    return 0
  fi
  for candidate in main master develop trunk; do
    if git -C "$path" show-ref --verify --quiet "refs/remotes/origin/$candidate" || \
       git -C "$path" show-ref --verify --quiet "refs/heads/$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  branch=$(git -C "$path" branch --show-current 2>/dev/null || true)
  if [ -n "$branch" ] && [ "$branch" != HEAD ]; then
    printf '%s\n' "$branch"
    return 0
  fi
  tf_error "Could not detect a default base branch. Pass --base explicitly."
  return 1
}

tf_normalize_base() {
  local base=${1:-}
  base=${base#origin/}
  [ -n "$base" ] || return 1
  git check-ref-format --branch "$base" >/dev/null 2>&1 || {
    tf_error "Invalid base branch: $base"
    return 1
  }
  printf '%s\n' "$base"
}

tf_base_ref() {
  local path=$1 base=$2
  if git -C "$path" show-ref --verify --quiet "refs/remotes/origin/$base"; then
    printf 'origin/%s\n' "$base"
  elif git -C "$path" show-ref --verify --quiet "refs/heads/$base"; then
    printf '%s\n' "$base"
  else
    tf_error "Base branch '$base' is not available locally or under origin/."
    return 1
  fi
}

tf_slugify() {
  local raw=${1:-} slug
  slug=$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9._/-]+/-/g; s/-+/-/g; s#^[/.-]+##; s#[/.-]+$##')
  slug=${slug:0:80}
  slug=${slug%%-}
  [ -n "$slug" ] || return 1
  printf '%s\n' "$slug"
}

tf_valid_branch() {
  git check-ref-format --branch "$1" >/dev/null 2>&1
}

tf_worktree_for_branch() {
  local path=$1 wanted=$2 current_path='' current_branch='' line
  while IFS= read -r line; do
    case "$line" in
      'worktree '*) current_path=${line#worktree } ;;
      'branch refs/heads/'*)
        current_branch=${line#branch refs/heads/}
        if [ "$current_branch" = "$wanted" ]; then
          printf '%s\n' "$current_path"
          return 0
        fi
        ;;
      '') current_path=''; current_branch='' ;;
    esac
  done < <(git -C "$path" worktree list --porcelain)
  return 1
}

tf_worktree_for_path() {
  local path=$1 wanted current_path='' line
  wanted=$(tf_abs_dir "$2") || return 1
  while IFS= read -r line; do
    case "$line" in
      'worktree '*)
        current_path=${line#worktree }
        current_path=$(tf_abs_dir "$current_path" 2>/dev/null || printf '%s' "$current_path")
        [ "$current_path" = "$wanted" ] && {
          printf '%s\n' "$current_path"
          return 0
        }
        ;;
    esac
  done < <(git -C "$path" worktree list --porcelain)
  return 1
}

tf_default_worktree_path() {
  local path=$1 branch=$2 project_parent project_name safe_branch
  project_parent=$(dirname "$path")
  project_name=$(basename "$path")
  safe_branch=$(printf '%s' "$branch" | tr '/' '-')
  printf '%s/.taskflow-worktrees/%s-%s\n' "$project_parent" "$project_name" "$safe_branch"
}

tf_task_state_dir() {
  local common
  common=$(tf_git_common_dir "$1") || return 1
  printf '%s/%s/tasks\n' "$common" "$TF_TASKFLOW_NAME"
}

tf_branch_state_file() {
  local path=$1 branch=$2 digest state_dir
  digest=$(printf '%s' "$branch" | tf_sha256) || return 1
  state_dir=$(tf_task_state_dir "$path") || return 1
  mkdir -p "$state_dir"
  printf '%s/%s.state\n' "$state_dir" "$digest"
}

tf_sha256() {
  if tf_command_exists shasum; then
    shasum -a 256 | awk '{print $1}'
  elif tf_command_exists sha256sum; then
    sha256sum | awk '{print $1}'
  else
    tf_error 'A SHA-256 utility is required (shasum or sha256sum).'
    return 1
  fi
}

tf_start() {
  local task='' requested_name='' base='' path='' base_ref branch slug explicit_name=false
  local existing state_file state_task candidate index=1 parent path_abs

  while [ $# -gt 0 ]; do
    case "$1" in
      --name)
        [ $# -ge 2 ] || { tf_error 'start --name requires a value.'; return 1; }
        requested_name=$2; explicit_name=true; shift 2 ;;
      --base)
        [ $# -ge 2 ] || { tf_error 'start --base requires a value.'; return 1; }
        base=$2; shift 2 ;;
      --path)
        [ $# -ge 2 ] || { tf_error 'start --path requires a value.'; return 1; }
        path=$2; shift 2 ;;
      -*) tf_error "start: unknown option $1"; return 1 ;;
      *)
        [ -z "$task" ] || { tf_error 'start accepts one plain task description.'; return 1; }
        task=$1; shift ;;
    esac
  done

  [ -n "$task" ] || { tf_error 'Usage: tf.sh start <plain task> [--name slug] [--base branch] [--path path]'; return 1; }
  tf_validate_project >/dev/null || return 1
  git -C "$TF_PROJECT_ROOT" fetch origin --prune || {
    tf_error 'Could not refresh origin before creating the task worktree.'
    return 1
  }
  [ -n "$base" ] || base=$(tf_default_base "$TF_PROJECT_ROOT") || return 1
  base=$(tf_normalize_base "$base") || return 1
  base_ref=$(tf_base_ref "$TF_PROJECT_ROOT" "$base") || return 1

  if [ "$explicit_name" = true ]; then
    branch=$(tf_slugify "$requested_name") || {
      tf_error "Could not derive a valid branch from --name '$requested_name'."
      return 1
    }
    tf_valid_branch "$branch" || { tf_error "Invalid branch name: $branch"; return 1; }
  else
    slug=$(tf_slugify "$task") || {
      tf_error 'The task text does not produce a valid branch slug; pass --name.'
      return 1
    }
    branch=$slug
    while git -C "$TF_PROJECT_ROOT" show-ref --verify --quiet "refs/heads/$branch" || \
          git -C "$TF_PROJECT_ROOT" show-ref --verify --quiet "refs/remotes/origin/$branch"; do
      if existing=$(tf_worktree_for_branch "$TF_PROJECT_ROOT" "$branch"); then
        break
      fi
      index=$((index + 1))
      branch="$slug-$index"
    done
  fi

  [ "$branch" != "$base" ] || {
    tf_error "Task branch '$branch' is the selected base branch; pass a different --name."
    return 1
  }

  if existing=$(tf_worktree_for_branch "$TF_PROJECT_ROOT" "$branch"); then
    existing=$(tf_abs_dir "$existing") || return 1
    if [ -n "$path" ]; then
      path_abs=$(cd "$(dirname "$path")" 2>/dev/null && pwd -P)/$(basename "$path") || true
      [ "$path_abs" = "$existing" ] || {
        tf_error "Branch '$branch' is already attached to $existing, not $path."
        return 1
      }
    fi
    tf_result "status=existing task=$task branch=$branch path=$existing base=$base"
    return 0
  fi

  if [ -n "$path" ]; then
    case "$path" in
      /*) path_abs=$path ;;
      *) path_abs=$PWD/$path ;;
    esac
    parent=$(dirname "$path_abs")
    mkdir -p "$parent"
    path_abs=$(cd "$parent" && pwd -P)/$(basename "$path_abs")
  else
    path_abs=$(tf_default_worktree_path "$TF_PROJECT_ROOT" "$branch")
    mkdir -p "$(dirname "$path_abs")"
  fi

  if [ -e "$path_abs" ]; then
    if tf_worktree_for_path "$TF_PROJECT_ROOT" "$path_abs" >/dev/null 2>&1; then
      tf_error "Path is already a registered worktree but not for branch '$branch': $path_abs"
    else
      tf_error "Refusing to use an existing unregistered path: $path_abs"
    fi
    return 1
  fi

  if git -C "$TF_PROJECT_ROOT" show-ref --verify --quiet "refs/heads/$branch"; then
    git -C "$TF_PROJECT_ROOT" worktree add "$path_abs" "$branch" >/dev/null
  elif git -C "$TF_PROJECT_ROOT" show-ref --verify --quiet "refs/remotes/origin/$branch"; then
    git -C "$TF_PROJECT_ROOT" worktree add --track -b "$branch" "$path_abs" "origin/$branch" >/dev/null
  else
    git -C "$TF_PROJECT_ROOT" worktree add --no-track -b "$branch" "$path_abs" "$base_ref" >/dev/null
  fi

  state_file=$(tf_branch_state_file "$TF_PROJECT_ROOT" "$branch") || return 1
  {
    printf 'task=%s\n' "$task"
    printf 'branch=%s\n' "$branch"
    printf 'base=%s\n' "$base"
    printf 'path=%s\n' "$path_abs"
  } > "$state_file"
  tf_result "status=created task=$task branch=$branch path=$path_abs base=$base"
}

tf_list() {
  tf_validate_project >/dev/null || return 1
  git -C "$TF_PROJECT_ROOT" worktree list --porcelain
}

tf_current_branch() {
  local path=${1:-.} branch
  branch=$(git -C "$path" branch --show-current 2>/dev/null || true)
  [ -n "$branch" ] && printf '%s\n' "$branch"
}

tf_require_non_base_branch() {
  local path=$1 base branch
  branch=$(tf_current_branch "$path") || true
  [ -n "$branch" ] || { tf_error "Detached HEAD is not a task branch."; return 1; }
  base=$(tf_default_base "$path") || return 1
  [ "$branch" != "$base" ] || {
    tf_error "Operation is not allowed directly on base branch '$base'."
    return 1
  }
  printf '%s\n' "$branch"
}

tf_commit() {
  local message='' root branch
  while [ $# -gt 0 ]; do
    case "$1" in
      --message|-m)
        [ $# -ge 2 ] || { tf_error 'commit --message requires a value.'; return 1; }
        message=$2; shift 2 ;;
      *) tf_error "commit: unknown argument $1"; return 1 ;;
    esac
  done
  [ -n "${message//[[:space:]]/}" ] || { tf_error 'commit requires --message text.'; return 1; }
  root=$(tf_validate_worktree "$PWD") || return 1
  branch=$(tf_require_non_base_branch "$root") || return 1
  git -C "$root" diff --cached --quiet && {
    tf_error 'Nothing is staged. Stage only task-owned files before running commit.'
    return 1
  }
  git -C "$root" commit -m "$message" >/dev/null
  tf_result "status=committed branch=$branch commit=$(git -C "$root" rev-parse HEAD)"
}

tf_find_python_roots() {
  local root=$1 candidate found=0
  while IFS= read -r candidate; do
    [ -n "$candidate" ] || continue
    printf '%s\n' "$(dirname "$candidate")"
    found=1
  done < <(find "$root" \
    \( -path "$root/.git" -o -path "$root/.venv" -o -path "$root/venv" -o -path "$root/node_modules" \
       -o -path "$root/.taskflow-worktrees" -o -path '*/__pycache__' -o -path '*/.tox' \) -prune -o \
    -type f \( -name pyproject.toml -o -name setup.py -o -name setup.cfg -o -name requirements.txt \
       -o -name 'requirements-*.txt' -o -name uv.lock -o -name poetry.lock -o -name Pipfile \) -print)

  if [ "$found" -eq 0 ]; then
    candidate=$(find "$root" \
      \( -path "$root/.git" -o -path "$root/.venv" -o -path "$root/venv" -o -path "$root/node_modules" \
         -o -path "$root/.taskflow-worktrees" -o -path '*/__pycache__' \) -prune -o \
      -type f -name '*.py' -print -quit)
    [ -n "$candidate" ] && printf '%s\n' "$root"
  fi
}

tf_find_js_roots() {
  local root=$1 candidate
  find "$root" \
    \( -path "$root/.git" -o -path "$root/node_modules" -o -path "$root/.venv" -o -path "$root/venv" \
       -o -path "$root/.taskflow-worktrees" -o -path '*/dist' -o -path '*/build' -o -path '*/.next' \) -prune -o \
    -type f -name package.json -print | while IFS= read -r candidate; do
      [ -n "$candidate" ] && dirname "$candidate"
    done
}

tf_has_python_tests() {
  local root=$1 test_file
  [ -d "$root/tests" ] && return 0
  test_file=$(find "$root" \
    \( -path "$root/.git" -o -path "$root/.venv" -o -path "$root/venv" -o -path "$root/node_modules" \
       -o -path '*/__pycache__' \) -prune -o -type f \
    \( -name 'test_*.py' -o -name '*_test.py' \) -print -quit)
  [ -n "$test_file" ]
}

tf_python_build_metadata() {
  local root=$1
  [ -f "$root/setup.py" ] && return 0
  [ -f "$root/pyproject.toml" ] || return 1
  grep -Eq '^[[:space:]]*\[build-system\][[:space:]]*$' "$root/pyproject.toml"
}

tf_python_env_kind() {
  local root=$1
  if [ -f "$root/uv.lock" ]; then
    printf 'uv\n'
  elif [ -f "$root/poetry.lock" ]; then
    printf 'poetry\n'
  else
    printf 'system\n'
  fi
}

tf_python_interpreter() {
  local root=$1
  if [ -x "$root/.venv/bin/python" ]; then
    printf '%s\n' "$root/.venv/bin/python"
  elif [ -n "${TASKFLOW_PYTHON:-}" ]; then
    printf '%s\n' "$TASKFLOW_PYTHON"
  else
    printf 'python3\n'
  fi
}

tf_python_tool_available() {
  local python_bin=$1 module=$2
  if [ -x "$python_bin" ] || tf_command_exists "$python_bin"; then
    "$python_bin" -c "import $module" >/dev/null 2>&1
  else
    return 1
  fi
}

tf_json_script_exists() {
  local package_json=$1 script_name=$2
  python3 - "$package_json" "$script_name" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        package = json.load(handle)
except (OSError, ValueError):
    raise SystemExit(2)
raise SystemExit(0 if sys.argv[2] in (package.get("scripts") or {}) else 1)
PY
}

tf_package_manager() {
  local root=$1 manager='npm' package_manager
  if [ -f "$root/pnpm-lock.yaml" ]; then
    manager=pnpm
  elif [ -f "$root/yarn.lock" ]; then
    manager=yarn
  elif [ -f "$root/bun.lockb" ] || [ -f "$root/bun.lock" ]; then
    manager=bun
  elif [ -f "$root/package-lock.json" ] || [ -f "$root/npm-shrinkwrap.json" ]; then
    manager=npm
  else
    package_manager=$(python3 - "$root/package.json" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        value = json.load(handle).get("packageManager", "")
except (OSError, ValueError):
    value = ""
print(value.split("@", 1)[0] if value else "")
PY
    ) || package_manager=''
    case "$package_manager" in
      npm|pnpm|yarn|bun) manager=$package_manager ;;
    esac
  fi
  printf '%s\n' "$manager"
}

tf_locked_package_manager() {
  local root=$1
  if [ -f "$root/pnpm-lock.yaml" ]; then
    printf 'pnpm\n'
  elif [ -f "$root/yarn.lock" ]; then
    printf 'yarn\n'
  elif [ -f "$root/bun.lockb" ] || [ -f "$root/bun.lock" ]; then
    printf 'bun\n'
  elif [ -f "$root/package-lock.json" ] || [ -f "$root/npm-shrinkwrap.json" ]; then
    printf 'npm\n'
  else
    return 1
  fi
}

tf_run_step() {
  local report=$1 label=$2 cwd=$3 command_text rc=0 argument
  shift 3
  command_text=''
  for argument in "$@"; do
    command_text="$command_text $(printf '%q' "$argument")"
  done
  {
    printf '\n## %s\n' "$label"
    printf 'cwd: %s\n' "$cwd"
    printf 'command:%s\n' "$command_text"
  } >> "$report"
  if (cd "$cwd" && "$@") >> "$report" 2>&1; then
    printf 'status: passed\n' >> "$report"
    return 0
  else
    rc=$?
    printf 'status: failed (%s)\n' "$rc" >> "$report"
    return "$rc"
  fi
}

tf_run_step_ci() {
  local report=$1 label=$2 cwd=$3 command_text rc=0 argument
  shift 3
  command_text='CI=1'
  for argument in "$@"; do
    command_text="$command_text $(printf '%q' "$argument")"
  done
  {
    printf '\n## %s\n' "$label"
    printf 'cwd: %s\n' "$cwd"
    printf 'command: %s\n' "$command_text"
  } >> "$report"
  if (cd "$cwd" && env CI=1 "$@") >> "$report" 2>&1; then
    printf 'status: passed\n' >> "$report"
    return 0
  else
    rc=$?
    printf 'status: failed (%s)\n' "$rc" >> "$report"
    return "$rc"
  fi
}

tf_report_skip() {
  local report=$1 label=$2 reason=$3
  {
    printf '\n## %s\n' "$label"
    printf 'status: skipped\nreason: %s\n' "$reason"
  } >> "$report"
}

tf_clean_js_outputs() {
  local root=$1 report=$2 output
  for output in dist build .next out coverage; do
    if [ -d "$root/$output" ] && [ ! -L "$root/$output" ]; then
      printf 'cleaned: %s\n' "$root/$output" >> "$report"
      rm -rf -- "$root/$output"
    fi
  done
}

tf_receipt_dir() {
  local path=$1 common
  common=$(tf_git_common_dir "$path") || return 1
  printf '%s/%s/receipts\n' "$common" "$TF_TASKFLOW_NAME"
}

tf_receipt_file() {
  local path=$1 commit=$2 directory
  directory=$(tf_receipt_dir "$path") || return 1
  mkdir -p "$directory"
  printf '%s/%s.receipt\n' "$directory" "$commit"
}

tf_receipt_value() {
  local receipt=$1 key=$2
  sed -n "s/^${key}=//p" "$receipt" | head -1
}

tf_verify() {
  local requested_path=${1:-.} root branch base commit receipt_dir receipt report failed=0
  local build_tmp candidate manager script has_any python_kind python_bin build_out
  local package_json
  local -a python_roots=() js_roots=() test_cmd=() ruff_cmd=() build_cmd=() install_cmd=()

  root=$(tf_validate_worktree "$requested_path") || return 1
  git -C "$root" diff --quiet && git -C "$root" diff --cached --quiet || {
    tf_error "Verification requires a clean worktree; commit or stash changes first."
    return 1
  }
  commit=$(git -C "$root" rev-parse HEAD 2>/dev/null) || {
    tf_error "Cannot verify an empty repository without a commit."
    return 1
  }
  branch=$(tf_current_branch "$root") || true
  [ -n "$branch" ] || branch=DETACHED
  base=$(tf_default_base "$root") || return 1
  receipt_dir=$(tf_receipt_dir "$root") || return 1
  mkdir -p "$receipt_dir"
  receipt="$receipt_dir/$commit.receipt"
  report="$receipt_dir/$commit.report"
  build_tmp=$(mktemp -d "${TMPDIR:-/tmp}/taskflow-build.XXXXXX")
  # Test discovery and lint tools must not leave pyc files in the task tree.
  export PYTHONDONTWRITEBYTECODE=1
  : > "$report"
  {
    printf 'TASKFLOW_VERIFICATION=1\n'
    printf 'status=running\n'
    printf 'commit=%s\nbranch=%s\nbase=%s\npath=%s\n' "$commit" "$branch" "$base" "$root"
    printf 'started_at=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  } >> "$report"

  while IFS= read -r candidate; do
    [ -n "$candidate" ] || continue
    case " ${python_roots[*]-} " in *" $candidate "*) ;; *) python_roots+=("$candidate") ;; esac
  done < <(tf_find_python_roots "$root")
  while IFS= read -r candidate; do
    [ -n "$candidate" ] || continue
    case " ${js_roots[*]-} " in *" $candidate "*) ;; *) js_roots+=("$candidate") ;; esac
  done < <(tf_find_js_roots "$root")

  if [ "${#python_roots[@]}" -eq 0 ]; then
    tf_report_skip "$report" 'Python test/lint/build' 'not detected: no Python package/source manifest or test path.'
  else
    for candidate in "${python_roots[@]}"; do
      python_kind=$(tf_python_env_kind "$candidate")
      python_bin=$(tf_python_interpreter "$candidate")
      test_cmd=()
      ruff_cmd=()
      build_cmd=()
      build_out="$build_tmp/$(printf '%s' "$candidate" | tf_sha256)"
      mkdir -p "$build_out"

      if ! tf_command_exists "$python_bin"; then
        printf '\n## Python interpreter\ncwd: %s\nstatus: failed\nreason: missing %s\n' "$candidate" "$python_bin" >> "$report"
        failed=1
        continue
      fi

      case "$python_kind" in
        uv)
          if tf_command_exists uv; then
            test_cmd=(uv run --frozen pytest)
            ruff_cmd=(uv run --frozen ruff)
            if tf_python_build_metadata "$candidate"; then
              build_cmd=(uv build --wheel --out-dir "$build_out")
            fi
          else
            printf '\n## uv toolchain\ncwd: %s\nstatus: failed\nreason: uv.lock requires the uv CLI; install uv or set up the project environment.\n' "$candidate" >> "$report"
            failed=1
          fi
          ;;
        poetry)
          if tf_command_exists poetry; then
            test_cmd=(poetry run pytest)
            ruff_cmd=(poetry run ruff)
            if tf_python_build_metadata "$candidate"; then
              build_cmd=(poetry build --format wheel --output "$build_out")
            fi
          else
            printf '\n## Poetry toolchain\ncwd: %s\nstatus: failed\nreason: poetry.lock requires the Poetry CLI; install Poetry or set up the project environment.\n' "$candidate" >> "$report"
            failed=1
          fi
          ;;
        system)
          if [ -x "$candidate/.venv/bin/pytest" ]; then
            test_cmd=("$candidate/.venv/bin/pytest")
          elif tf_python_tool_available "$python_bin" pytest; then
            test_cmd=("$python_bin" -m pytest)
          elif tf_command_exists pytest; then
            test_cmd=(pytest)
          fi
          if [ -x "$candidate/.venv/bin/ruff" ]; then
            ruff_cmd=("$candidate/.venv/bin/ruff")
          elif tf_python_tool_available "$python_bin" ruff; then
            ruff_cmd=("$python_bin" -m ruff)
          elif tf_command_exists ruff; then
            ruff_cmd=(ruff)
          fi
          if tf_python_build_metadata "$candidate" && tf_python_tool_available "$python_bin" build; then
            build_cmd=("$python_bin" -m build --wheel --outdir "$build_out" --no-isolation)
          fi
          ;;
      esac

      if tf_has_python_tests "$candidate"; then
        if [ "${#test_cmd[@]}" -eq 0 ]; then
          printf '\n## Python tests\ncwd: %s\nstatus: failed\nreason: pytest tests were detected, but no project pytest runner is available. Unittest fallback is intentionally disabled.\n' "$candidate" >> "$report"
          failed=1
        elif ! tf_run_step "$report" 'Python tests' "$candidate" "${test_cmd[@]}"; then
          failed=1
        fi
      else
        tf_report_skip "$report" 'Python tests' "not detected under $candidate (no tests/ or test_*.py/*_test.py)."
      fi
      if [ "${#ruff_cmd[@]}" -eq 0 ]; then
        printf '\n## Ruff\ncwd: %s\nstatus: failed\nreason: Ruff is required for Python lint and format checks, but no project runner is available.\n' "$candidate" >> "$report"
        failed=1
      else
        if ! tf_run_step "$report" 'Ruff check' "$candidate" "${ruff_cmd[@]}" check .; then failed=1; fi
        if ! tf_run_step "$report" 'Ruff format check' "$candidate" "${ruff_cmd[@]}" format --check .; then failed=1; fi
      fi
      if tf_python_build_metadata "$candidate"; then
        if [ "${#build_cmd[@]}" -eq 0 ]; then
          printf '\n## Python package build\ncwd: %s\nstatus: failed\nreason: package build metadata ([build-system] or setup.py) was detected, but no clean build runner is available.\n' "$candidate" >> "$report"
          failed=1
        elif ! tf_run_step "$report" 'Python package build' "$candidate" "${build_cmd[@]}"; then
          failed=1
        fi
      else
        build_cmd=(env PYTHONPYCACHEPREFIX="$build_out/pycache" "$python_bin" -m compileall -q "$candidate")
        if ! tf_run_step "$report" 'Python compile fallback' "$candidate" "${build_cmd[@]}"; then failed=1; fi
      fi
    done
  fi

  if [ "${#js_roots[@]}" -eq 0 ]; then
    tf_report_skip "$report" 'React/JavaScript checks' 'not detected: no package.json discovered.'
  else
    for candidate in "${js_roots[@]}"; do
      package_json="$candidate/package.json"
      has_any=0
      for script in lint test build clean; do
        if tf_json_script_exists "$package_json" "$script"; then has_any=1; fi
      done
      if [ "$has_any" -eq 0 ]; then
        tf_report_skip "$report" 'React/JavaScript checks' "not detected: no lint, test, build, or clean scripts in $package_json."
        continue
      fi
      if ! manager=$(tf_locked_package_manager "$candidate"); then
        printf '\n## JavaScript frozen dependency install\ncwd: %s\nstatus: failed\nreason: package scripts exist but no supported lockfile was found; add package-lock.json, pnpm-lock.yaml, yarn.lock, or bun.lock before verification.\n' "$candidate" >> "$report"
        failed=1
        continue
      fi
      if ! tf_command_exists "$manager"; then
        printf '\n## JavaScript package manager\ncwd: %s\nstatus: failed\nreason: lockfile selects %s, but that CLI is unavailable.\n' "$candidate" "$manager" >> "$report"
        failed=1
        continue
      fi
      case "$manager" in
        npm) install_cmd=(npm ci) ;;
        pnpm) install_cmd=(pnpm install --frozen-lockfile) ;;
        yarn) install_cmd=(yarn install --frozen-lockfile) ;;
        bun) install_cmd=(bun install --frozen-lockfile) ;;
      esac
      if ! tf_run_step "$report" 'JavaScript frozen dependency install' "$candidate" "${install_cmd[@]}"; then failed=1; fi
      if tf_json_script_exists "$package_json" clean; then
        if ! tf_run_step "$report" 'JavaScript clean script' "$candidate" "$manager" run clean; then failed=1; fi
      fi
      tf_clean_js_outputs "$candidate" "$report"
      if tf_json_script_exists "$package_json" lint; then
        if ! tf_run_step "$report" 'JavaScript lint' "$candidate" "$manager" run lint; then failed=1; fi
      else
        tf_report_skip "$report" 'JavaScript lint' 'not configured: no lint script declared.'
      fi
      if tf_json_script_exists "$package_json" test; then
        if ! tf_run_step_ci "$report" 'JavaScript tests' "$candidate" "$manager" run test; then failed=1; fi
      else
        tf_report_skip "$report" 'JavaScript tests' 'not configured: no test script declared.'
      fi
      if tf_json_script_exists "$package_json" build; then
        if ! tf_run_step "$report" 'JavaScript clean build' "$candidate" "$manager" run build; then failed=1; fi
      else
        tf_report_skip "$report" 'JavaScript clean build' 'not configured: no build script declared.'
      fi
    done
  fi

  if [ -n "$(git -C "$root" status --porcelain)" ]; then
    printf '\n## Repository cleanliness after verification\nstatus: failed\nreason: verification generated tracked or untracked changes.\n' >> "$report"
    failed=1
  else
    printf '\n## Repository cleanliness after verification\nstatus: passed\n' >> "$report"
  fi
  printf 'finished_at=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" >> "$report"

  if [ "$failed" -ne 0 ]; then
    sed -i.bak 's/^status=running$/status=failed/' "$report" 2>/dev/null || true
    rm -f -- "$report.bak"
    cp "$report" "$receipt.failed"
    rm -rf -- "$build_tmp"
    tf_error "Verification failed. Report: $receipt.failed"
    tf_result "status=failed commit=$commit branch=$branch report=$receipt.failed"
    return 1
  fi

  sed -i.bak 's/^status=running$/status=passed/' "$report" 2>/dev/null || true
  rm -f -- "$report.bak"
  cp "$report" "$receipt"
  rm -rf -- "$build_tmp"
  tf_result "status=passed commit=$commit branch=$branch base=$base receipt=$receipt"
}

tf_doctor() {
  local root remote forge base errors=0 host
  if ! root=$(tf_validate_project); then return 1; fi
  printf 'project=%s\n' "$root"
  if ! tf_command_exists git; then tf_error 'Required command is missing: git'; errors=$((errors + 1)); fi
  if ! tf_command_exists python3; then tf_error 'Required command is missing: python3'; errors=$((errors + 1)); fi
  if ! remote=$(tf_remote_url "$root"); then
    tf_error 'The repository has no origin remote.'
    errors=$((errors + 1))
  else
    host=$(tf_remote_host "$remote")
    printf 'origin=%s\nhost=%s\n' "$remote" "$host"
  fi
  if base=$(tf_default_base "$root"); then printf 'default_base=%s\n' "$base"; else errors=$((errors + 1)); fi
  if forge=$(tf_detect_forge "$root"); then
    printf 'forge=%s\n' "$forge"
    if [ "$forge" = github ]; then
      if ! tf_command_exists gh; then tf_error 'GitHub forge selected but gh is missing.'; errors=$((errors + 1));
      elif ! gh auth status >/dev/null 2>&1; then tf_error 'gh is not authenticated.'; errors=$((errors + 1)); fi
    else
      if ! tf_command_exists glab; then tf_error 'GitLab forge selected but glab is missing.'; errors=$((errors + 1));
      elif ! glab auth status >/dev/null 2>&1; then tf_error 'glab is not authenticated.'; errors=$((errors + 1)); fi
    fi
  else
    errors=$((errors + 1))
  fi
  if [ "$errors" -eq 0 ]; then tf_result 'status=ok'; else tf_result "status=failed errors=$errors"; return 1; fi
}

tf_json_pr_record() {
  python3 -c '
import json
import sys

try:
    records = json.load(sys.stdin)
except (ValueError, TypeError):
    raise SystemExit(1)
if isinstance(records, dict):
    records = [records]
for record in records:
    number = record.get("number") or record.get("iid") or ""
    url = record.get("url") or record.get("web_url") or ""
    if number:
        print(f"{number}\t{url}")
        break
'
}

tf_extract_url() {
  grep -Eo 'https?://[^[:space:]"<>]+' | tail -1 | sed -E 's/[),.;]+$//' || true
}

tf_github_open_pr() {
  local branch=$1 base=$2 data
  data=$(gh pr list --head "$branch" --base "$base" --state open --json number,url --limit 1) || return 1
  printf '%s\n' "$data" | tf_json_pr_record
}

tf_gitlab_open_mr() {
  local branch=$1 base=$2 data
  data=$(glab mr list --source-branch "$branch" --target-branch "$base" --state opened --output json) || return 1
  printf '%s\n' "$data" | tf_json_pr_record
}

tf_open_browser() {
  local url=$1
  if tf_command_exists open; then
    if ! open "$url" >/dev/null 2>&1; then tf_warn "Could not open browser for $url"; fi
  elif tf_command_exists xdg-open; then
    if ! xdg-open "$url" >/dev/null 2>&1; then tf_warn "Could not open browser for $url"; fi
  else
    tf_warn "No browser opener found; open this URL manually: $url"
  fi
}

tf_require_forge_auth() {
  local forge=$1
  if [ "$forge" = github ]; then
    tf_command_exists gh || { tf_error 'GitHub requires the gh CLI.'; return 1; }
    gh auth status >/dev/null 2>&1 || { tf_error 'gh is not authenticated.'; return 1; }
  else
    tf_command_exists glab || { tf_error 'GitLab requires the glab CLI.'; return 1; }
    glab auth status >/dev/null 2>&1 || { tf_error 'glab is not authenticated.'; return 1; }
  fi
}

tf_write_mr_body() {
  local body_file=$1 receipt=$2 branch=$3 commit=$4 base=$5 output=$6
  {
    cat "$body_file"
    printf '\n\n--- Taskflow delivery metadata ---\n'
    printf 'Branch: %s\nCommit: %s\nBase: %s\nVerification: passed\n' "$branch" "$commit" "$base"
    printf '\n--- Exact taskflow verification receipt ---\n'
    cat "$receipt"
  } > "$output"
}

tf_ship() {
  local title='' body_file='' base='' root branch commit receipt forge body_tmp existing iid url remote
  while [ $# -gt 0 ]; do
    case "$1" in
      --title)
        [ $# -ge 2 ] || { tf_error 'ship --title requires a value.'; return 1; }
        title=$2; shift 2 ;;
      --body-file)
        [ $# -ge 2 ] || { tf_error 'ship --body-file requires a path.'; return 1; }
        body_file=$2; shift 2 ;;
      --base)
        [ $# -ge 2 ] || { tf_error 'ship --base requires a branch.'; return 1; }
        base=$2; shift 2 ;;
      *) tf_error "ship: unknown argument $1"; return 1 ;;
    esac
  done
  [ -n "${title//[[:space:]]/}" ] || { tf_error 'ship requires --title text.'; return 1; }
  [ -n "$body_file" ] || { tf_error 'ship requires --body-file path.'; return 1; }
  body_file=$(tf_abs_file "$body_file") || { tf_error "MR body file not found: $body_file"; return 1; }
  root=$(tf_validate_worktree "$PWD") || return 1
  branch=$(tf_current_branch "$root") || true
  [ -n "$branch" ] || { tf_error 'Cannot ship a detached HEAD.'; return 1; }
  [ -n "$base" ] || base=$(tf_default_base "$root") || return 1
  base=$(tf_normalize_base "$base") || return 1
  [ "$branch" != "$base" ] || { tf_error "Cannot ship base branch '$base'."; return 1; }
  git -C "$root" fetch origin --prune || {
    tf_error 'Could not refresh origin before shipping.'
    return 1
  }
  tf_base_ref "$root" "$base" >/dev/null || return 1
  git -C "$root" merge-base --is-ancestor "origin/$base" HEAD || {
    tf_error "Task branch is not based on the current origin/$base; reconcile it before shipping."
    return 1
  }
  [ "$(git -C "$root" rev-list --count "origin/$base..HEAD")" -gt 0 ] || {
    tf_error "Task branch has no commits ahead of origin/$base."
    return 1
  }
  [ -z "$(git -C "$root" status --porcelain)" ] || {
    tf_error 'Ship requires a clean worktree. Commit or stash changes first.'
    return 1
  }
  commit=$(git -C "$root" rev-parse HEAD) || return 1
  receipt=$(tf_receipt_file "$root" "$commit") || return 1
  [ -f "$receipt" ] || {
    tf_error "No passing verification receipt for HEAD $commit. Run tf.sh verify first."
    return 1
  }
  [ "$(tf_receipt_value "$receipt" status)" = passed ] || {
    tf_error "Verification receipt for HEAD $commit is not passing."
    return 1
  }
  [ "$(tf_receipt_value "$receipt" commit)" = "$commit" ] || {
    tf_error 'Verification receipt commit does not match HEAD.'
    return 1
  }
  [ "$(tf_receipt_value "$receipt" branch)" = "$branch" ] || {
    tf_error "Verification receipt branch does not match '$branch'; rerun verify here."
    return 1
  }
  [ "$(tf_receipt_value "$receipt" base)" = "$base" ] || {
    tf_error "Verification receipt base does not match '$base'; rerun verify with the current base."
    return 1
  }

  forge=$(tf_detect_forge "$root") || return 1
  tf_require_forge_auth "$forge" || return 1
  remote=$(tf_remote_url "$root") || return 1
  body_tmp=$(mktemp "${TMPDIR:-/tmp}/taskflow-mr-body.XXXXXX")
  tf_write_mr_body "$body_file" "$receipt" "$branch" "$commit" "$base" "$body_tmp"
  git -C "$root" push --set-upstream origin "$branch" || {
    rm -f -- "$body_tmp"
    return 1
  }

  if [ "$forge" = github ]; then
    existing=$(tf_github_open_pr "$branch" "$base") || {
      tf_error 'Could not query open GitHub pull requests for this branch.'
      rm -f -- "$body_tmp"
      return 1
    }
    if [ -n "$existing" ]; then
      iid=${existing%%$'\t'*}
      url=${existing#*$'\t'}
      gh pr edit "$iid" --title "$title" --body-file "$body_tmp" >/dev/null || {
        rm -f -- "$body_tmp"
        return 1
      }
      [ -n "$url" ] || url=$(gh pr view "$iid" --json url --jq .url 2>/dev/null || true)
      [ -n "$url" ] || { tf_error 'GitHub updated the PR but returned no URL.'; rm -f -- "$body_tmp"; return 1; }
      tf_open_browser "$url"
      rm -f -- "$body_tmp"
      tf_result "status=updated forge=github branch=$branch base=$base commit=$commit url=$url remote=$remote"
      return 0
    fi
    url=$(gh pr create --head "$branch" --base "$base" --title "$title" --body-file "$body_tmp" 2>&1 | tf_extract_url)
    [ -n "$url" ] || { tf_error 'GitHub did not return a PR URL.'; rm -f -- "$body_tmp"; return 1; }
  else
    existing=$(tf_gitlab_open_mr "$branch" "$base") || {
      tf_error 'Could not query open GitLab merge requests for this branch.'
      rm -f -- "$body_tmp"
      return 1
    }
    if [ -n "$existing" ]; then
      iid=${existing%%$'\t'*}
      url=${existing#*$'\t'}
      glab mr update "$iid" --title "$title" --description "$(<"$body_tmp")" >/dev/null || {
        rm -f -- "$body_tmp"
        return 1
      }
      [ -n "$url" ] || url=$(glab mr view "$iid" --output json 2>/dev/null | tf_extract_url)
      [ -n "$url" ] || { tf_error 'GitLab updated the MR but returned no URL.'; rm -f -- "$body_tmp"; return 1; }
      tf_open_browser "$url"
      rm -f -- "$body_tmp"
      tf_result "status=updated forge=gitlab branch=$branch base=$base commit=$commit url=$url remote=$remote"
      return 0
    fi
    url=$(glab mr create --source-branch "$branch" --target-branch "$base" \
      --title "$title" --description "$(<"$body_tmp")" --yes 2>&1 | tf_extract_url)
    [ -n "$url" ] || { tf_error 'GitLab did not return an MR URL.'; rm -f -- "$body_tmp"; return 1; }
  fi
  tf_open_browser "$url"
  rm -f -- "$body_tmp"
  tf_result "status=created forge=$forge branch=$branch base=$base commit=$commit url=$url remote=$remote"
}
