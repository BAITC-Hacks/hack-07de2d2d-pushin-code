# Change request template

The taskflow driver fills this template into the provider-native pull request or merge request.
Keep commands and outcomes exact; do not replace failed checks with a general statement.

## Task

<Original plain-language task>

## Goal and acceptance criteria

- Goal: <what should be true after this change>
- [ ] <criterion>
- [ ] <criterion>

## Summary

<Short explanation of the implementation and user-visible behavior.>

## Changed files and areas

- `<path>` — <what changed and why>
- `<path>` — <what changed and why>

## Verification

| Scope | Exact command (from package root) | Result |
| --- | --- | --- |
| Backend tests | `<pytest command>` | `PASS` / `FAIL` / `not detected` |
| Frontend tests | `<package-manager test command>` | `PASS` / `FAIL` / `not detected` |
| Clean backend build | `<python -m build or python -m compileall command>` | `PASS` / `FAIL` / `not detected` |
| Clean frontend build | `<package-manager build command>` | `PASS` / `FAIL` / `not detected` |
| Python lint | `ruff check .` | `PASS` / `FAIL` / `not detected` |
| Python format | `ruff format --check .` | `PASS` / `FAIL` / `not detected` |
| Other project checks | `<exact command>` | `PASS` / `FAIL` / `not detected` |

<Include every detected package/workspace. Explain any `not detected` result with inspected paths.>

## Assumptions and non-goals

- Assumptions: <explicit assumptions>
- Non-goals: <what this change intentionally does not do>

## Risks and follow-up

- Risks/limitations: <known risks, operational notes, or remaining uncertainty>
- Follow-up: <optional next step, or `None`>

## Delivery metadata

- Worktree: `<absolute path>`
- Branch: `<branch>`
- Commit: `<SHA>`
- Base/target: `<base branch>`
- Forge: `GitHub` / `GitLab`
- Review: `<URL, populated by ship>`
