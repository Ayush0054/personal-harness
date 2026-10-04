# Agno Harness

A local coding-agent loop using Agno, with persistent tasks, steering, and completion review.

## Install

Requires Python 3.10+ and macOS or Linux. From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r agno/requirements.txt
export OPENAI_API_KEY="your-api-key"
```

Installs **Agno**, the **OpenAI SDK**, **SQLAlchemy**, and **Pydantic**.

## Start

```bash
python agno/cli.py --workspace /path/to/project \
  start "Add CSV export to the report page" \
  --done-when "The report page can export its data as CSV" \
  --max-turns 8
```

The command prints a task ID. Use the same workspace for subsequent commands:

```bash
python agno/cli.py --workspace /path/to/project status TASK_ID
python agno/cli.py --workspace /path/to/project steer TASK_ID "Use the existing export utility"
python agno/cli.py --workspace /path/to/project run TASK_ID
python agno/cli.py --workspace /path/to/project accept TASK_ID
```

`accept` marks completion after you review the evidence. Add `--async` before the command for async execution. Use `--model MODEL_ID` to select a model.

Shell is disabled by default. `--shell` enables commands with individual approval; tests require explicit authorization. State is saved in `<workspace>/.context/agent-loop/`.

Prototype; compatibility with published Agno releases has not been tested. See [design and comparison](DESIGN.md) and [test status](TEST_LOG.md).
