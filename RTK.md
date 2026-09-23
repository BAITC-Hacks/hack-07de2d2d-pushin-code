# Repository task workflow

This is the canonical task-routing and delivery guide for this directory. It is intentionally
independent of external planning systems and of a particular hosting provider. A plain-language task is normalized
into a local task record; the task record, Git history, and the provider review are the sources of
truth.

## Prerequisite

Before any task work, this directory must be its own Git repository (or a linked worktree of that
repository) with an `origin` remote. An empty directory that merely discovers a parent/home
`.git` is not a valid checkout. Run:

```bash
bash scripts/taskflow/tf.sh doctor
```

Resolve a missing checkout or remote first. The driver must fail closed rather than create a
worktree from an unrelated parent repository.

## Intake and ownership

- Accept the user's plain task text. Do not require, infer, or call an external planning system.
- `task-worker` owns one task's implementation in one isolated worktree.
- `delivery-reviewer` audits the diff and verification evidence, then gates delivery.
- The task's normalized goal, acceptance criteria, non-goals, assumptions, risks, worktree path,
  branch, and commit SHA are recorded in taskflow state and copied into the review body.
- A task may be resumed after interruption. A repeated `start` for the same normalized task must
  return the existing task/worktree rather than create a second one.

## Driver contract

Use `bash scripts/taskflow/tf.sh`; it owns stateful Git mechanics and provider calls:

```text
doctor  check repository, origin, required tools, selected forge, and local state
start   create or resume one isolated worktree for the supplied plain task
list    show task/worktree/branch/review state
commit  commit staged task-owned changes with an explicit imperative message
verify  run the complete detected test, clean-build, and lint/format matrix
ship    push origin, create/update the review, populate its body, and open its URL
```

The driver may expose options for these commands; use its help and preserve the command names.
Never replace it with hand-written `git worktree`, branch, push, `gh`, or `glab` calls.

Forge selection is deterministic. Use `TASKFLOW_FORGE=github` or `TASKFLOW_FORGE=gitlab` when
auto-detection from the `origin` URL is insufficient, including self-hosted/custom domains. Once
selected, use only the matching authenticated CLI (`gh` for GitHub or `glab` for GitLab) for that
delivery. Do not call both CLIs or use external planning-system integrations. `ship` updates an existing
open review for the same repository, base, and head instead of creating a duplicate.

## Worktree rules

1. Run `doctor`, then `start` with the exact task text.
2. Work only in the path returned by `start`; the checkout root is an orchestration area.
3. Branch names and worktree names are deterministic slugs of the task. No prefix convention is
   required; names only need to be safe, unique, and resumable.
4. Keep unrelated user changes untouched. Stage only files owned by this task.
5. Use `list` to recover the path after a restart. Never attach a second worktree to the same task.
6. Do not work on protected/default branches directly.

## Stack-aware implementation and verification

The workflow supports a Python/FastAPI backend and a React frontend in one repository or in
separate packages. `verify` discovers manifests and test scripts rather than assuming one stack.
It must run every detected backend and frontend test suite, then the clean-build and Python lint
gates below. Report exact commands and exit status in the review body.

### Python/FastAPI

- Detect Python packages from `pyproject.toml`, `requirements*.txt`, `uv.lock`, `poetry.lock`,
  `Pipfile`, or Python source/tests.
- Run the repository/package's complete `pytest` suite (prefer the declared project runner, then
  `uv run pytest`, then `python -m pytest`). Do not narrow to one test unless the task explicitly
  changes the verification scope.
- Run `ruff check .` and `ruff format --check .` from each applicable project root. Ruff is the
  selected Python lint and formatting tool; an applicable Python project without an available
  Ruff installation is a failed prerequisite, not a silently skipped check.
- Run a clean Python build/syntax gate. Remove only declared/generated build outputs, then run the
  configured package build when present (`python -m build` or the declared build script), and at
  minimum compile all project packages with `python -m compileall` when no package build exists.
- Exercise FastAPI tests through the project's existing test configuration, including async and
  HTTP-client tests. Do not start production services or use production credentials.

### React/frontend

- Detect every `package.json` and lockfile. Use the lockfile's package manager (`npm`, `pnpm`,
  `yarn`, or `bun`) for a frozen install and each package's declared non-interactive test script.
- Run all detected frontend tests in CI mode (for example, the project's `test -- --runInBand`
  or `test -- --watch=false` form), preserving the package's own script and arguments.
- Clean only generated frontend outputs/caches, then run each declared production `build` script.
  A missing build script is recorded as `not configured`; a configured build that fails is a hard
  failure.
- A React package with runnable scripts must have a lockfile. Use `npm ci` or the corresponding
  frozen install so a fresh worktree is reproducible; never rewrite the lockfile.

### Missing suites and failures

If no tests are detected for a stack, record `not detected` with the paths/manifests inspected;
never call that result passing. A detected suite, clean build, Ruff check, or format check that
fails blocks commit delivery until fixed or explicitly reported to the user. Verification must
run against the committed tree as well as the pre-commit tree.

## Commit, review, and browser handoff

After implementation:

1. Stage only task files and run `commit` with an imperative summary.
2. Run `verify` against that commit. If generated files changed, inspect, clean, or commit the
   intentional result and verify again; never ship a dirty or unverified commit.
3. Run `ship`. It pushes the current branch to `origin`, creates or updates one pull request or
   merge request using the selected forge CLI, fills the body from
   `docs/change-request-template.md`, and attempts to open the review URL.
4. Report the exact URL even when the OS browser command is unavailable or fails. Shipping never
   merges, approves, closes, or promotes the review.

The body must include the original task, concise summary, changed files/areas, exact verification
commands and outcomes (all backend/frontend tests, clean build, `ruff check`, and
`ruff format --check`), assumptions/non-goals, risks/limitations, worktree/branch, commit SHA,
and any follow-up. The source template is `docs/change-request-template.md`.

## Safety

- No external planning-system operations.
- No production access, deployment, merge, force-push, or destructive cleanup.
- Never use credentials from outside the task's configured development environment.
- If the repository, origin, selected forge CLI, or required test/lint tool is unavailable, stop
  before external writes and report the blocking prerequisite.
