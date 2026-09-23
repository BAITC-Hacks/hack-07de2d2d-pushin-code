# Task workflow

This repository accepts plain-language tasks and delivers one provider-native review per task.
There is no external planning-system step. The task worker implements in an isolated worktree; the
delivery reviewer checks the committed evidence; `ship` hands the review to the user without
merging it.

## Before the first task

This directory must be a real checkout with its own `.git` and an `origin` remote. A directory
that inherits a parent/home `.git` is not sufficient. Once the checkout is present, run:

```bash
bash scripts/taskflow/tf.sh doctor
```

For a self-hosted or ambiguous remote, select exactly one forge:

```bash
TASKFLOW_FORGE=github bash scripts/taskflow/tf.sh doctor
TASKFLOW_FORGE=gitlab bash scripts/taskflow/tf.sh doctor
```

The selected forge determines the only CLI used for review delivery (`gh` or `glab`).

## Lifecycle

```text
plain task
    -> doctor
    -> start or resume one worktree
    -> implement and inspect
    -> run all detected tests + clean builds + Ruff
    -> commit
    -> verify the committed tree
    -> push origin and create/update one PR/MR
    -> open URL for the user (URL is reported if browser open fails)
```

The stable task identity comes from normalized task text. `start` resumes its existing worktree on
retry. Worktree and branch names are safe slugs and have no required prefix.

## Driver commands

Run every command from the repository root unless `start` returns a worktree path for the next
phase. Options are driver-defined; use `--help` when needed.

```bash
bash scripts/taskflow/tf.sh doctor
bash scripts/taskflow/tf.sh start "<plain task text>"
bash scripts/taskflow/tf.sh list
bash scripts/taskflow/tf.sh commit --message "<imperative summary>"
bash scripts/taskflow/tf.sh verify
bash scripts/taskflow/tf.sh ship
```

`commit` and `verify` operate on the active task worktree. The driver owns branch/worktree
creation, commit metadata, remote push, provider selection, review lookup, body update, and browser
handoff. Do not substitute direct `git worktree`, branch, push, `gh`, or `glab` commands.

## Verification matrix

`verify` discovers project manifests, workspaces, lockfiles, test scripts, and CI conventions. It
runs every applicable suite, not only tests adjacent to changed files.

| Area | Required checks |
| --- | --- |
| Python/FastAPI | Full `pytest` suite per detected package; clean package build when configured, otherwise `python -m compileall`; `ruff check .`; `ruff format --check .` |
| React/frontend | Frozen lockfile install; every detected test script in non-watch mode; clean declared production `build` scripts using the lockfile's package manager |
| Other configured projects | Their declared complete test/build commands, recorded exactly |

If no suite is present, record `not detected` and the inspected manifests; that is not a passing
test result. A configured suite or required tool that fails blocks shipping. Verification is
recorded against the exact committed SHA that will be delivered.

## Review contents and retry behavior

`ship` renders `docs/change-request-template.md` with:

- the original task and normalized acceptance criteria;
- summary and changed files/areas;
- exact commands, package roots, and outcomes for all tests, clean builds, Ruff lint, and format;
- assumptions, non-goals, risks, limitations, and follow-up;
- worktree, branch, commit SHA, target branch, and selected forge.

For a retry, `ship` finds the open review matching repository, base, and head and updates its body
and source branch. It does not create duplicates. It never merges, approves, deploys, or closes a
review.

## Recovery

Use `list` to find a task's worktree after an interrupted session. Keep working there and rerun
`verify` after any fix. If the browser command is unavailable, copy the printed URL to the user.
If repository ownership, `origin`, forge authentication, or required test/lint tools are missing,
stop before external writes and resolve the prerequisite.
