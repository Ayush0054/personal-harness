"""Run a standalone local coding harness: python cli.py --help."""

import argparse
import asyncio
from pathlib import Path

from loop import AgentLoop, LoopEvent, TurnDecision

from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.models.openai import OpenAIResponses
from agno.run.requirement import RunRequirement
from agno.tools.coding import CodingTools


def _make_loop(args: argparse.Namespace) -> AgentLoop:
    workspace = args.workspace.resolve()
    state_dir = args.state_dir.resolve() if args.state_dir else workspace / ".context" / "agent-loop"
    instructions = [
        "You are a coding agent working on one operator objective. Use tools to make concrete progress.",
        "Read relevant code before editing. Preserve unrelated local changes.",
        "Before editing a directory, read applicable ancestor and nested AGENTS.md instructions.",
        "Do not execute tests unless the operator explicitly authorizes testing in the task or steering.",
        "Use blocked when you need a credential, a decision, or external input; describe exactly what is needed.",
        "Never invent validation results. Cite observed file contents and tool outcomes in completion evidence.",
        "On recovery inspect the workspace and recorded outcomes before considering another mutation.",
        "Project instructions and operator constraints apply across every turn.",
    ]
    for name in ("AGENTS.md", ".cursorrules"):
        path = workspace / name
        if path.is_file():
            text = path.read_text()
            if len(text) > 32_000:
                raise ValueError(f"Project instruction file is too large: {path}")
            instructions.append(f"Project instructions from {path}:\n{text}")
    model = OpenAIResponses(id=args.model) if args.model else OpenAIResponses()
    agent = Agent(
        id="standalone-coding-loop",
        name="Coding Loop",
        model=model,
        db=SqliteDb(id="coding-loop-sessions", db_file=str(state_dir / "sessions.sqlite3")),
        tools=[CodingTools(
            base_dir=workspace,
            enable_run_shell=args.shell,
            enable_grep=True,
            enable_find=True,
            enable_ls=True,
            requires_confirmation_tools=["run_shell"],
        )],
        instructions=instructions,
        output_schema=TurnDecision,
        add_history_to_context=True,
        num_history_runs=5,
        store_events=True,
        tool_call_limit=args.tool_call_limit,
        retries=0,
    )
    return AgentLoop(agent=agent, workspace=workspace, state_file=state_dir / "loop.sqlite3")


def _print_event(event: LoopEvent) -> None:
    if event.kind == "task_status":
        state = event.payload
        decision = state.get("decision") or {}
        print(f"\nTurn {state['turns_started']}: {state['status']} — {decision.get('summary', '')}", flush=True)
    elif event.kind == "agno_event":
        native = event.payload
        if native.get("event") in ("ToolCallStarted", "ToolCallCompleted", "ToolCallError"):
            tool = native.get("tool") or {}
            print(f"{native['event']}: {tool.get('tool_name', '')}", flush=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A standalone task loop powered by one reusable Agno agent.")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--state-dir", type=Path, help="Default: <workspace>/.context/agent-loop")
    parser.add_argument("--model", help="OpenAI model; defaults to this checkout's OpenAIResponses default")
    parser.add_argument("--shell", action="store_true", help="Expose shell commands with native per-call confirmation")
    parser.add_argument("--tool-call-limit", type=int, default=12)
    parser.add_argument("--async", dest="use_async", action="store_true", help="Use Agno's native async runner")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="Persist a task and begin execution")
    start.add_argument("objective")
    start.add_argument("--done-when", action="append", required=True, dest="criteria")
    start.add_argument("--max-turns", type=int, default=10)
    start.add_argument("--user-id", default="local")
    run = commands.add_parser("run", help="Continue a task with pending work")
    run.add_argument("task_id")
    run.add_argument("--recover", action="store_true", help="Continue after inspecting an interrupted or failed run")
    run.add_argument("--additional-turns", type=int, default=0)
    commands.add_parser("status", help="Show task state and exact outstanding requirements").add_argument("task_id")
    events = commands.add_parser("events", help="Read persisted events after a sequence cursor")
    events.add_argument("task_id")
    events.add_argument("--after", type=int, default=0)
    steer = commands.add_parser("steer", help="Queue input for the next outer turn")
    steer.add_argument("task_id")
    steer.add_argument("message")
    commands.add_parser("accept", help="Accept the completion evidence after reviewing it").add_argument("task_id")
    approve = commands.add_parser("approve", help="Resolve all listed confirmation requests and resume the same run")
    approve.add_argument("task_id")
    approve.add_argument("decision", choices=("allow", "deny"))
    return parser


def _main() -> None:
    parser = _parser()
    args = parser.parse_args()
    if args.tool_call_limit < 1:
        parser.error("--tool-call-limit must be positive")
    loop = _make_loop(args)
    if args.command == "status":
        print(loop.inspect(args.task_id).model_dump_json(indent=2))
        return
    if args.command == "events":
        for event in loop.events(args.task_id, after=args.after):
            print(event.model_dump_json())
        return
    if args.command == "steer":
        loop.steer(args.task_id, args.message)
        print("Steering queued. An idle task needs an explicit run command.")
        return
    if args.command == "accept":
        print(loop.accept(args.task_id).model_dump_json(indent=2))
        return
    requirements = None
    if args.command == "start":
        state = loop.create(args.objective, args.criteria, user_id=args.user_id, max_turns=args.max_turns)
        task_id = state.task_id
        print(f"Task: {task_id}", flush=True)
    else:
        task_id = args.task_id
    if args.command == "approve":
        state = loop.inspect(task_id)
        if state.status != "awaiting_approval":
            parser.error("Task is not awaiting approval")
        requirements = [RunRequirement.from_dict(data) for data in state.requirements]
        if not requirements or any(not req.needs_confirmation for req in requirements):
            parser.error("This CLI resolves confirmation requests only; use the Python API for other requirement types")
        for requirement in requirements:
            if args.decision == "allow":
                requirement.confirm()
            else:
                requirement.reject()
    options = dict(
        recover=getattr(args, "recover", False), additional_turns=getattr(args, "additional_turns", 0),
        requirements=requirements, on_event=_print_event,
    )
    state = asyncio.run(loop.arun(task_id, **options)) if args.use_async else loop.run(task_id, **options)
    print(state.model_dump_json(indent=2))


if __name__ == "__main__":
    try:
        _main()
    except KeyboardInterrupt:
        print("\nInterrupted. Inspect task status and tool outcomes before using run --recover.")
