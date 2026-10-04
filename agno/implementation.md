# Implementation

- [x] Inspect Agno's native sync/async streaming loop, statuses, tool APIs, and requirement continuation.
- [x] Compare the seven requested project/version targets using official documentation and source snapshots.
- [x] Implement task identity, typed decisions, explicit completion criteria, and operator acceptance.
- [x] Persist task checkpoints and task events together; persist steering admission separately.
- [x] Serialize runner access through a local OS lock.
- [x] Reuse one Agno agent and native session history across outer turns.
- [x] Stream native lifecycle events and return assembled task state.
- [x] Add sync/async execution and control APIs.
- [x] Resume native paused runs with resolved requirements and the same run ID.
- [x] Stop on failure/cancellation, repeated identical continuation traces, or turn-budget exhaustion.
- [x] Add CLI, usage cookbook, architecture comparison, and explicit limitations.
- [ ] Run meaningful tests after the user authorizes testing.
- [ ] Run formatting/validation after authorization for checks.
- [ ] Validate the live provider/tool/approval flow after authorization.

Future slices: model-window-aware compaction; steering between native model calls; strict cost/token/deadline controls; command idempotency and side-effect reconciliation; Postgres control state; provider runtime adapters; task-specific sandbox integration.
