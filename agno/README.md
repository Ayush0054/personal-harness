# Agent Loop

A standalone local coding harness built on one reusable Agno `Agent`. It persists an objective, completion criteria, operator steering, run IDs, task status, and a replayable event trail. Agno supplies the existing model/tool loop and conversation store.

See [DESIGN.md](DESIGN.md) for the comparison of OpenCode v1/v2, T3 Code, Claude Agent SDK, Codex SDK, DeepSeek Harness, and Prime Agent, with source links and the first version's limits.

## Start a task

Use a Python environment containing Agno, OpenAI, and SQLAlchemy, with `OPENAI_API_KEY` configured. This prototype was written against the local Agno checkout; compatibility with a published Agno release has not been tested. To use that checkout, install it into your environment with `pip install -e /path/to/agno/libs/agno openai sqlalchemy`. The example uses the installed `OpenAIResponses` default unless `--model` is supplied. The commands below are usage examples; they have not been run.

From the repository root:

```bash
python agno/cli.py \
  --workspace /path/to/project \
  start "Implement a CSV export for the existing report page" \
  --done-when "The page exposes CSV export using its current UI components" \
  --done-when "The final report names the changed files and states which checks were run" \
  --max-turns 8
```

The CLI prints a task ID before starting model work, streams tool lifecycle messages, and prints the final task state. Files are editable through `CodingTools`; shell commands are disabled by default. Tests require explicit operator authorization in the task or steering.

State defaults to `<workspace>/.context/agent-loop/`:

- `loop.sqlite3`: task checkpoints, events, and steering inbox.
- `sessions.sqlite3`: native Agno conversation/run state.
- `loop.lock`: OS-managed runner lock, automatically released on process exit.

Use a different `--state-dir` if needed. Keep the same workspace, state directory, model, and tool settings when resuming a task. The local state files are not an authentication boundary and should be kept out of Git.

## Inspect, steer, and continue

Substitute the task ID printed by `start`. These examples assume the current directory is the task workspace; otherwise retain the original `--workspace` and `--state-dir` options.

```bash
python /path/to/personal-harness/agno/cli.py status TASK_ID
python /path/to/personal-harness/agno/cli.py events TASK_ID --after 0
python /path/to/personal-harness/agno/cli.py steer TASK_ID "Use the existing export utility. Do not run tests yet."
python /path/to/personal-harness/agno/cli.py run TASK_ID
```

`steer` persists a message. An active runner takes it at its next outer-turn boundary; an idle task needs an explicit `run`. Steering does not cancel active tools. A blocked or stalled task needs steering explaining how to proceed. A budget-limited task needs an explicit extension:

```bash
python /path/to/personal-harness/agno/cli.py run TASK_ID --additional-turns 3
```

After a failed or interrupted run, inspect task events, the native Agno run, and workspace changes before explicitly recovering:

```bash
python /path/to/personal-harness/agno/cli.py run TASK_ID --recover
```

Recovery starts another outer turn in the same session. It does not replay the previous run's tool calls. Uncertain side effects still require operator judgment. A dispatched interrupted turn counts against the budget.

## Shell confirmations

`--shell` enables `CodingTools.run_shell` with Agno's native confirmation requirement. A task pauses as `awaiting_approval`; `status` displays the actual tool names/arguments in its stored requirements. Inspect them before choosing a decision:

```bash
python /path/to/personal-harness/agno/cli.py --shell status TASK_ID
python /path/to/personal-harness/agno/cli.py --shell approve TASK_ID allow
python /path/to/personal-harness/agno/cli.py --shell approve TASK_ID deny
```

Each approval command resolves all currently listed confirmation requirements and continues the same Agno run. The CLI does not resolve other native requirement types. Programmatic callers can reconstruct `RunRequirement` objects from `state.requirements`, supply user input or external results, and pass the resolved list to `run(..., requirements=...)` or `arun(...)`.

Shell confirmation does not grant automatic testing permission. Explicitly authorize testing in a task or steering message when desired, and then review the proposed command. This is a supervised local harness, not an OS sandbox.

## Completion

The model returns `TurnDecision(outcome, summary, next_action, evidence)`. A `complete` claim requires exactly one nonempty evidence entry for each indexed completion criterion and becomes `review_required`. Inspect that evidence and the resulting artifacts, then accept:

```bash
python /path/to/personal-harness/agno/cli.py accept TASK_ID
```

Acceptance records `completed`. It cannot happen while operator steering is pending. If further work is needed before acceptance, queue steering and run again. Accepted tasks are final; create another task for a new objective.

## Async and customization

Put `--async` before the command to use native `Agent.arun()` / `Agent.acontinue_run()`. The harness offers paired sync/async APIs for creation, inspection, event reads, steering, acceptance, and execution. Its observer callback is synchronous and should be fast; observer failures are logged without aborting execution.

`AgentLoop` accepts an existing Agno agent, an existing workspace directory, and a state-file path. The agent must have a stable ID and persistent database. Configure its model, tools, prompts, and per-run limits once, then reuse it. If tools can pause, configure `output_schema=TurnDecision` on the agent as the CLI does, so native paused-run continuations retain the same output contract.

## Verification status

Implementation and source comparison only. No tests, cookbook runs, linting, formatting scripts, or model calls have been performed. See [TEST_LOG.md](TEST_LOG.md) and [implementation.md](implementation.md).
