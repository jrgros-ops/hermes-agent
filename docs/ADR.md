# Architecture Decision Records

This document captures durable architectural decisions for the Hermes
agent. Each entry is a self-contained record of a context, a decision,
and its consequences.

## ADR-0001: Strict-Readonly Kanban Workers

**Status:** Accepted

### Context

Kanban workers can be dispatched autonomously to act on a task. Some
workflows require an autonomous worker that is **explicitly confined
to its own task workspace** — a worker that can write files but cannot
escape that workspace, modify the repository, reach the user's profile,
or interact with arbitrary external systems.

A prior self-improvement review identified that an autonomous worker
without a strict capability could, in principle, mutate the repository
under itself, escalate by writing profile config, or traverse symlinks
to escape the workspace. The narrow solution is a strict-readonly
capability that activates only when the dispatcher explicitly opts in.

### Decision

- Persist an explicit `strict_readonly` capability on the Kanban task
  row (`INTEGER NOT NULL DEFAULT 0`). Provenance is not capability;
  `created_by='agent'` does NOT imply strict mode.
- Propagate the capability through the dispatcher:
  `hermes_cli/kanban_db._default_spawn` reads `task.strict_readonly` and
  pins `HERMES_KANBAN_STRICT_READONLY=1` in the worker subprocess env.
- Remove `terminal` and `code_execution` from the worker CLI
  `--toolsets` allowlist by filtering at the resolved-toolsets level
  (the production CLI accepts a comma-separated `--toolsets` allowlist
  but does not accept `--disabled-toolsets`).
- Preserve the `kanban` toolset on the strict worker so the worker can
  self-complete via `kanban_complete`.
- Treat the Hermes session_id (`task_id` argument on file tools) and
  the Kanban task_id (`HERMES_KANBAN_TASK`) as **separate identities**.
  The gate never compares them.
- Never trust `HERMES_KANBAN_WORKSPACE` alone. The gate authenticates
  the workspace against the persisted task.
- Compute the authoritative expected workspace via
  `hermes_cli.kanban_db.expected_workspace_for_task(conn, task_id,
  board)` — a pure read-only resolver — and require it to canonicalise
  equal to the pinned workspace.
- Apply canonical path containment on the target: resolve both sides
  via `Path.resolve(strict=False)` and assert
  `target.is_relative_to(workspace)`.
- Fail closed on missing, malformed, or mismatched authority. No soft
  fallback, no cwd fallback, no `$HOME` fallback.
- Promote declared completion artifacts through the trusted completion
  path (`kanban_db._persist_scratch_completion_artifacts` →
  `_insert_completion_attachment` with `uploaded_by='kanban_complete'`).
  The LLM does not re-emit file contents; production code reads bytes
  from the existing workspace file.
- Suppress automatic background review (`_spawn_background_review`) on
  the strict worker session via the Scope A `skip_background_review`
  propagation so no `skill_view` / `skill_manage` events fire after the
  worker turn.

### Consequences

- Strict workers can mutate only their authorized task workspace
  through `write_file_tool` and `patch_tool`. The repository, profile,
  configuration, reports, skills, other-task workspaces, and external
  paths are not writable by the strict worker.
- Ordinary Kanban and non-Kanban behavior remains outside this
  capability. Non-strict workers see no effect.
- The dispatcher, the Kanban database, and the completion path remain
  trusted components. The file-tool gate is downstream of those three.
- Runtime validation (real-process worker against production binary)
  is required for security-sensitive changes to this capability.
- The strict capability is opt-in per task. Operators must explicitly
  set `strict_readonly=True` (or `--strict-readonly` on the CLI) to
  activate it.

### References

- `docs/security/strict-readonly-kanban-workers.md` — full
  architectural description.
- `hermes_cli/kanban_db.py` — `expected_workspace_for_task`,
  `_default_spawn`, `_persist_scratch_completion_artifacts`,
  `_insert_completion_attachment`.
- `tools/file_tools.py` — `_strict_readonly_gate` and supporting
  helpers.
- `hermes_cli/kanban.py` — `--strict-readonly` CLI flag.
- `agent/autonomy/initiator.py` — `strict_readonly` objective field.
- `tests/agent/test_skip_background_review.py` — Scope A
  `skip_background_review` propagation.
