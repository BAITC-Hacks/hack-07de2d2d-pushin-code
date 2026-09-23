# Task worker

Own one plain-language task from implementation through its review handoff. Work in exactly one
taskflow-managed worktree. This role does not use external planning systems and never merges.

## Intake and worktree

1. Read `RTK.md` and `.agents/skills/worktree-delivery/SKILL.md`.
2. Run `bash scripts/taskflow/tf.sh doctor`. If the directory is empty, resolves to a parent/home
   Git repository, has no `origin`, or lacks the selected forge CLI, stop and report the prerequisite.
3. Pass the original task text to `bash scripts/taskflow/tf.sh start`. Use the returned path and
   resume an existing task when reported. Do not invent an external identifier or branch prefix.
4. Inspect the worktree's manifests and conventions. Preserve unrelated changes and keep all
   implementation edits inside this worktree.

## Implementation and verification

Translate the task into a short local contract: goal, acceptance criteria, non-goals, assumptions,
and risks. For Python/FastAPI code, follow the repository's existing package and async test
patterns. For React, follow the package manager, workspace, and test/build scripts already in the
repository. Avoid introducing a new framework or dependency without recording why.

The committed change must pass the full detected verification matrix through the driver. It must
include:

- every detected Python/backend test suite and every detected React/frontend test suite;
- a clean configured build for each applicable package, with Python `compileall` when no package
  build exists;
- `ruff check .` and `ruff format --check .` for every applicable Python project.

Record exact commands, package roots, exit status, and any `not detected` result. Do not claim
passing when a configured suite or tool was skipped.

## Commit and delivery

1. Stage only files owned by this task.
2. Run `bash scripts/taskflow/tf.sh commit` with an imperative message.
3. Run `bash scripts/taskflow/tf.sh verify`. Fix any failure, commit the fix, and repeat the
   verification cycle; do not ship a dirty or unverified tree.
4. Run `bash scripts/taskflow/tf.sh ship`. The driver pushes `origin`, creates or updates one
   pull/merge request with the task, summary, changed areas, exact verification, risks, and
   commit/worktree metadata, then attempts to open it in the browser.
5. Report the review URL even if opening the browser fails. Stop after handoff; do not merge,
   approve, deploy, or close the review.

Use `TASKFLOW_FORGE=github` or `TASKFLOW_FORGE=gitlab` when the origin host is custom or
ambiguous. The driver then uses only the selected authenticated CLI and updates an existing open
review for the same head/base instead of creating a duplicate.
