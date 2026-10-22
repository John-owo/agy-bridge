# Handoff for an agy run

Use this record when ownership of a CC or Codex task using agy moves to another parent agent. It adds run-specific evidence to the [canonical cross-agent handoff protocol](https://github.com/John-owo/codex-cc-handoff/blob/main/codex/skills/agent-handoff/SKILL.md); follow that protocol for the rest of the task handoff.

## Release rule

The parent that dispatched an agy run owns it until an explicit release and accepted handoff. Before marking a handoff **READY**, settle every agy task you own: its CLI call has ended, `status.json` is terminal, and the run lock is absent. Record whether integration is complete or is being transferred as pending work. Do not mark a handoff ready while an agy process may still write. If an operation is still running or you cannot verify that it stopped, mark the handoff **BLOCKED**, identify the operation and what must settle, and leave it with its current owner. Do not launch a duplicate run from the receiving parent or automatically resume work after a handoff or quota reset.

The bridge does not persist the CLI process ID. Record the CLI `conversation_id` from `result.json`'s `raw_result` when provided (or the raw `stdout.txt`); a preflight or process failure may not have one. If no ID is available, say so. Record the active CLI PID when you tracked it; otherwise write `unknown` and state how its process state was checked. A `RUNNING` status or existing `.run.lock` means the run is not released. If process state or status cannot be reconciled, keep the handoff blocked.

## Record

Copy this template into the task handoff or work log. Use exact absolute paths and factual status; do not include credentials or secrets.

```text
Task / scope:
Outgoing owner:
Receiving owner:
Release: READY | BLOCKED
Recorded at (with timezone):
Blocker and settlement condition (if BLOCKED):

Source repository path:
Source branch / HEAD (if Git):
Run ID (UUID directory name):
Run directory (absolute path):
Staged workspace (absolute path):
Task input path: <RUN_DIR>\task-input.txt
Bridge script path:
CLI conversation_id: <result.json raw_result.conversation_id | not provided by CLI>
Active CLI PID: <PID | unknown>
Process verification: <how checked, result, and time; required when PID is unknown>

Run status: <status.json state, plus result.json state/reason if present>
Selected model and quota group: <selection.json>
Billing evidence: <selection.json billing snapshot; actual verification time if separately recorded, otherwise unknown>
  This is historical state only; it does not authorize another call or spending.

Run artifacts:
  status.json: <absolute path>
  result.json: <absolute path or not created>
  selection.json: <absolute path or not created>
  manifest.json: <absolute path>
  stdout.txt: <absolute path or not created>
  stderr.txt: <absolute path or not created>
  task-input.txt: <absolute path or not created>

Changes versus manifest: <created / modified / deleted paths from inspect>
Pending integration: <what remains; receiving parent must review before applying>
Source drift: <current source files compared with manifest source hashes; exact paths and result>
Checks actually run: <commands, outcomes, evidence paths>
Checks not run / remaining uncertainty:
Next step under current user authorization:
```

## Receiving the run

The receiving parent reads the exact run directory and workspace named above, then runs `python BRIDGE inspect --run-dir "RUN_DIR"`. Review `status.json`, `result.json`, `selection.json`, `manifest.json`, `stdout.txt`, `stderr.txt`, and `task-input.txt` when present. Confirm the outgoing owner released the run; reconcile any `RUNNING` state, lock, or PID evidence before another mutation or call.

Compare current source hashes at the recorded source path with the `source_sha256` values in `manifest.json` before integrating. Review changes reported by `inspect`, reconcile any source drift, and run applicable checks in the original project. A successful agy result, prior billing snapshot, or handoff record does not replace review, authorize a new agy call, or expand the user's current authorization. Continue only within that authorization; ask for new direction if the next action is outside it.
