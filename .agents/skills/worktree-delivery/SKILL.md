---
name: worktree-delivery
description: Implement a plain-language task in one isolated Git worktree, run complete Python/FastAPI and React verification, and deliver one GitHub pull request or GitLab merge request without external planning-system integration.
---

# Worktree delivery

Use this skill for every implementation task in this repository. Read `RTK.md` and the selected
role under `.agents/roles/` completely before acting. This skill is the routing and safety
contract; `bash scripts/taskflow/tf.sh` is the only workflow driver.

## Required sequence

1. Accept the plain task text. Normalize its goal, acceptance criteria, non-goals, assumptions,
   and risks locally. Do not ask for or infer an external planning-system identifier.
2. Run `bash scripts/taskflow/tf.sh doctor`. Fail closed if this directory is not an independent
   Git checkout with an `origin` remote, or if the selected forge CLI/tooling is unavailable.
3. Run `bash scripts/taskflow/tf.sh start` with the task. Reuse the returned worktree when the task
   already exists; do not create a second worktree. The generated name needs no prefix.
4. Implement and test only in the returned worktree. Preserve unrelated changes and stage only
   task-owned files.
5. Stage only task-owned files and run `bash scripts/taskflow/tf.sh commit` with an imperative
   summary.
6. Run `bash scripts/taskflow/tf.sh verify` against the committed tree. A failed or
   unrecorded check blocks shipping.
7. Run `bash scripts/taskflow/tf.sh ship`. It pushes `origin`, creates or updates one review on
   the selected forge, fills the review body, and attempts to open the URL. It never merges.
8. Return the worktree, branch, commit SHA, exact verification results, and review URL. If browser
   opening fails, still return the URL.

## Verification contract

`verify` must discover and execute all applicable suites, not only tests related to the edited
files:

- Python/FastAPI: complete `pytest` suite per detected package; `ruff check .`;
  `ruff format --check .`; clean package build when configured, otherwise `python -m compileall`.
- React: a frozen install from the package lockfile, every detected test script in non-watch mode,
  and clean declared production `build` scripts using the lockfile's package manager.
- Any additional repository-native test/build scripts discovered from project manifests or CI
  configuration.

Record `not detected` with evidence when a suite is absent; do not label absence as passing. A
missing required tool or failing configured command is blocking. The review body must list exact
commands, package roots, and outcomes.

## Forge and review rules

Infer GitHub versus GitLab from `origin` only when unambiguous. Set `TASKFLOW_FORGE=github` or
`TASKFLOW_FORGE=gitlab` for self-hosted/custom domains or to make the choice explicit. Use only
the selected authenticated CLI (`gh` or `glab`). On retry, locate the open review for the same
head/base and update it; never create a duplicate. The review body must be based on
`docs/change-request-template.md` and include task, summary, changes, exact verification, risks,
and handoff metadata. Browser-open failure does not hide the URL.

## Forbidden actions

Do not call an external planning system, link a review to one, work in the repository root,
hand-create a branch/worktree when the driver supports it, bypass failed verification, force-push,
merge, or access production.
