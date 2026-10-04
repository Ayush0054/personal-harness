"""A local task harness around Agno's existing model/tool loop."""

import asyncio
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from time import time
from typing import Any, Callable, Iterator, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from agno.agent import Agent
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.requirement import RunRequirement
from agno.utils.log import log_warning


class Evidence(BaseModel):
    criterion_index: int = Field(ge=0, description="Zero-based index of the completion criterion.")
    observation: str = Field(min_length=1, description="Concrete file, artifact, or tool result supporting this criterion.")


class TurnDecision(BaseModel):
    outcome: Literal["continue", "complete", "blocked"]
    summary: str = Field(min_length=1)
    next_action: str = Field(description="Next useful action, or the specific input needed if blocked.")
    evidence: list[Evidence] = Field(default_factory=list)


TaskStatus = Literal[
    "ready", "running", "awaiting_approval", "blocked", "review_required",
    "completed", "budget_exhausted", "stalled", "interrupted", "failed",
]


class TaskState(BaseModel):
    version: Literal[1] = 1
    task_id: str
    session_id: str
    user_id: str
    agent_id: str
    workspace: str
    objective: str = Field(min_length=1)
    criteria: list[str] = Field(min_length=1)
    status: TaskStatus = "ready"
    max_turns: int = Field(default=10, ge=1)
    turns_started: int = 0
    active_run_id: Optional[str] = None
    decision: Optional[TurnDecision] = None
    requirements: list[dict[str, Any]] = Field(default_factory=list)
    operator_messages: list[str] = Field(default_factory=list)
    last_fingerprint: Optional[str] = None
    stagnant_turns: int = 0
    note: str = ""


class LoopEvent(BaseModel):
    task_id: str
    kind: str
    timestamp: float = Field(default_factory=time)
    sequence: Optional[int] = None
    payload: dict[str, Any] = Field(default_factory=dict)


class _Store:
    """Short local transactions; task checkpoints and their events commit together."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS loop_tasks (id TEXT PRIMARY KEY, state TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS loop_events "
                "(sequence INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, event TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS loop_inbox "
                "(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, message TEXT NOT NULL);"
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _load(self, task_id: str) -> TaskState:
        with self._connection() as db:
            row = db.execute("SELECT state FROM loop_tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown task: {task_id}")
        return TaskState.model_validate_json(row[0])

    @staticmethod
    def _insert_event(db: sqlite3.Connection, event: LoopEvent) -> LoopEvent:
        cursor = db.execute(
            "INSERT INTO loop_events (task_id, event) VALUES (?, ?)", (event.task_id, event.model_dump_json())
        )
        event.sequence = cursor.lastrowid
        return event

    def _checkpoint(self, state: TaskState, kind: str, **payload: Any) -> LoopEvent:
        with self._connection() as db:
            db.execute(
                "INSERT INTO loop_tasks (id, state) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET state = excluded.state",
                (state.task_id, state.model_dump_json()),
            )
            event = self._insert_event(db, LoopEvent(task_id=state.task_id, kind=kind, payload=payload))
        return event

    def _append(self, event: LoopEvent) -> LoopEvent:
        with self._connection() as db:
            return self._insert_event(db, event)

    def _enqueue(self, task_id: str, message: str) -> None:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM loop_tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown task: {task_id}")
            if TaskState.model_validate_json(row[0]).status == "completed":
                raise ValueError("Create a new task for work after accepted completion.")
            db.execute("INSERT INTO loop_inbox (task_id, message) VALUES (?, ?)", (task_id, message))
            self._insert_event(db, LoopEvent(task_id=task_id, kind="steering_queued", payload={"message": message}))

    def _has_input(self, task_id: str) -> bool:
        with self._connection() as db:
            return db.execute("SELECT 1 FROM loop_inbox WHERE task_id = ? LIMIT 1", (task_id,)).fetchone() is not None

    def _begin_turn(self, state: TaskState) -> list[str]:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            messages = [row[0] for row in db.execute(
                "SELECT message FROM loop_inbox WHERE task_id = ? ORDER BY id", (state.task_id,)
            )]
            db.execute("DELETE FROM loop_inbox WHERE task_id = ?", (state.task_id,))
            state.status = "running"
            state.turns_started += 1
            state.active_run_id = str(uuid4())
            if messages:
                state.operator_messages.extend(messages)
                state.stagnant_turns = 0
                state.last_fingerprint = None
            db.execute("UPDATE loop_tasks SET state = ? WHERE id = ?", (state.model_dump_json(), state.task_id))
            self._insert_event(db, LoopEvent(task_id=state.task_id, kind="turn_started", payload={
                "turn": state.turns_started, "run_id": state.active_run_id, "steering": messages,
            }))
        return messages

    def _events(self, task_id: str, after: int) -> list[LoopEvent]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT sequence, event FROM loop_events WHERE task_id = ? AND sequence > ? ORDER BY sequence",
                (task_id, after),
            ).fetchall()
        return [LoopEvent.model_validate_json(raw).model_copy(update={"sequence": sequence}) for sequence, raw in rows]

    def _accept(self, state: TaskState) -> TaskState:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM loop_inbox WHERE task_id = ? LIMIT 1", (state.task_id,)).fetchone():
                raise ValueError("Pending steering must be processed before accepting completion.")
            state.status = "completed"
            db.execute("UPDATE loop_tasks SET state = ? WHERE id = ?", (state.model_dump_json(), state.task_id))
            self._insert_event(db, LoopEvent(task_id=state.task_id, kind="completion_accepted"))
        return state


class AgentLoop:
    """One reusable Agent, serialized execution, and durable task control on a local machine.

    Steering is admitted between Agno runs, not between individual model calls.
    Model-reported completion requires explicit operator acceptance.
    """

    def __init__(self, agent: Agent, workspace: Path, state_file: Path):
        if agent.db is None or not agent.id:
            raise ValueError("The agent needs a persistent database and a stable id.")
        self.agent = agent
        self.workspace = workspace.resolve()
        if not self.workspace.is_dir():
            raise ValueError("Workspace must be an existing directory.")
        self._store = _Store(state_file)

    @contextmanager
    def _ownership(self) -> Iterator[None]:
        # A process crash releases flock. The durable running status still requires explicit recovery.
        import fcntl

        with self._store.path.with_suffix(".lock").open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("This harness already has an active runner.") from exc
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def create(self, objective: str, criteria: list[str], user_id: str = "local", max_turns: int = 10) -> TaskState:
        """Persist an objective without starting model work."""
        if not objective.strip() or not user_id.strip() or not criteria or any(not item.strip() for item in criteria):
            raise ValueError("Objective, user id, and every completion criterion must be nonempty.")
        state = TaskState(
            task_id=str(uuid4()), session_id=str(uuid4()), user_id=user_id, agent_id=self.agent.id,
            workspace=str(self.workspace), objective=objective, criteria=criteria, max_turns=max_turns,
        )
        self._store._checkpoint(state, "task_created", objective=objective, criteria=criteria)
        return state

    async def acreate(self, objective: str, criteria: list[str], user_id: str = "local", max_turns: int = 10) -> TaskState:
        return await asyncio.to_thread(self.create, objective, criteria, user_id, max_turns)

    def inspect(self, task_id: str) -> TaskState:
        state = self._store._load(task_id)
        if state.workspace != str(self.workspace) or state.agent_id != self.agent.id:
            raise ValueError("Task workspace or agent id does not match this harness.")
        return state

    async def ainspect(self, task_id: str) -> TaskState:
        return await asyncio.to_thread(self.inspect, task_id)

    def events(self, task_id: str, after: int = 0) -> list[LoopEvent]:
        self.inspect(task_id)
        return self._store._events(task_id, after)

    async def aevents(self, task_id: str, after: int = 0) -> list[LoopEvent]:
        return await asyncio.to_thread(self.events, task_id, after)

    def steer(self, task_id: str, message: str) -> None:
        """Queue input for the next outer turn; this never interrupts a running tool."""
        self.inspect(task_id)
        if not message.strip():
            raise ValueError("Steering must be nonempty.")
        self._store._enqueue(task_id, message)

    async def asteer(self, task_id: str, message: str) -> None:
        await asyncio.to_thread(self.steer, task_id, message)

    def accept(self, task_id: str) -> TaskState:
        """Record the operator's acceptance of the proposed completion evidence."""
        with self._ownership():
            state = self.inspect(task_id)
            if state.status != "review_required":
                raise ValueError("Only a task awaiting completion review can be accepted.")
            return self._store._accept(state)

    async def aaccept(self, task_id: str) -> TaskState:
        return await asyncio.to_thread(self.accept, task_id)

    @staticmethod
    def _notify(event: LoopEvent, on_event: Optional[Callable[[LoopEvent], None]]) -> None:
        if on_event:
            try:
                on_event(event)
            except Exception as exc:
                log_warning(f"Loop observer failed: {exc}")

    def _prepare(self, task_id: str, recover: bool, additional_turns: int) -> TaskState:
        state = self.inspect(task_id)
        if additional_turns < 0:
            raise ValueError("Additional turns cannot be negative.")
        if state.status == "completed":
            return state
        if state.status in ("running", "interrupted", "failed"):
            if not recover:
                raise ValueError("Inspect the last run and workspace, then explicitly set recover=True.")
            state.note = (
                f"Explicit recovery after {state.status}; previous run {state.active_run_id}. "
                "Inspect existing outcomes before acting. Do not repeat an operation whose outcome is uncertain."
            )
            state.status = "ready"
            self._store._checkpoint(state, "recovery_requested", previous_run_id=state.active_run_id)
        if additional_turns:
            state.max_turns += additional_turns
            self._store._checkpoint(state, "budget_extended", max_turns=state.max_turns)
        if state.status in ("blocked", "stalled", "review_required") and self._store._has_input(task_id):
            state.status = "ready"
        if state.status == "budget_exhausted" and state.turns_started < state.max_turns:
            state.status = "ready"
        return state

    def _next_prompt(self, state: TaskState) -> Optional[str]:
        if state.status != "ready":
            return None
        if state.turns_started >= state.max_turns:
            state.status = "budget_exhausted"
            self._store._checkpoint(state, "budget_exhausted", turns=state.turns_started)
            return None
        self._store._begin_turn(state)
        return json.dumps({
            "objective": state.objective,
            "completion_criteria": [{"index": i, "criterion": item} for i, item in enumerate(state.criteria)],
            "turn": state.turns_started,
            "turn_limit": state.max_turns,
            "previous_decision": state.decision.model_dump() if state.decision else None,
            "operator_steering": state.operator_messages,
            "recovery_note": state.note,
            "instruction": (
                "Perform useful work with tools, then return a TurnDecision. Continue when there is work left; "
                "blocked means you need specific external input. Complete requires concrete evidence for every "
                "criterion. Evidence must say when checks were not run; never claim unperformed validation. "
                "The operator reviews completion. Follow project instructions and operator constraints."
            ),
        })

    def _observe(self, state: TaskState, item: Any, on_event: Optional[Callable[[LoopEvent], None]]) -> None:
        event = LoopEvent(task_id=state.task_id, kind="agno_event", payload=item.to_dict())
        # Text fragments are presentation data. The native Agno run stores the assembled response.
        if item.event not in ("RunContent", "ReasoningContentDelta", "RunIntermediateContent"):
            event = self._store._append(event)
        self._notify(event, on_event)

    def _finish(self, state: TaskState, output: Optional[RunOutput]) -> None:
        if output is None:
            raise RuntimeError("Agno stream ended without a final RunOutput.")
        if output.status == RunStatus.paused:
            state.status = "awaiting_approval"
            state.requirements = [req.to_dict() for req in output.active_requirements]
            self._store._checkpoint(state, "approval_required", requirements=state.requirements)
            return
        state.requirements = []
        if output.status == RunStatus.cancelled:
            state.status = "interrupted"
            state.note = "Agno cancelled the run. Inspect tool outcomes before explicit recovery."
        elif output.status != RunStatus.completed:
            state.status = "failed"
            state.note = str(output.content or "Agno did not complete the run.")
        else:
            if isinstance(output.content, TurnDecision):
                decision = output.content
            elif isinstance(output.content, str):
                decision = TurnDecision.model_validate_json(output.content)
            else:
                decision = TurnDecision.model_validate(output.content)
            state.decision = decision
            state.note = ""
            if decision.outcome == "complete":
                indices = [e.criterion_index for e in decision.evidence if e.observation.strip()]
                if sorted(indices) != list(range(len(state.criteria))):
                    raise ValueError("Completion needs one nonempty evidence entry for each criterion.")
                state.status = "review_required"
            elif decision.outcome == "blocked":
                if not decision.next_action.strip():
                    raise ValueError("Blocked output must specify the input needed.")
                state.status = "blocked"
            else:
                if not decision.next_action.strip():
                    raise ValueError("Continuation must specify the next useful action.")
                trace = [(tool.tool_name, tool.tool_args, tool.result) for tool in output.tools or []]
                fingerprint = hashlib.sha256(json.dumps(
                    trace or decision.next_action.strip(), sort_keys=True, default=str,
                ).encode()).hexdigest()
                state.stagnant_turns = state.stagnant_turns + 1 if fingerprint == state.last_fingerprint else 1
                state.last_fingerprint = fingerprint
                state.status = "stalled" if state.stagnant_turns >= 3 else "ready"
            if self._store._has_input(state.task_id):
                state.status = "ready"
        self._store._checkpoint(state, "turn_settled", status=state.status,
                                decision=state.decision.model_dump() if state.decision else None, note=state.note)

    def _resume_kwargs(self, state: TaskState, requirements: Optional[list[RunRequirement]]) -> Optional[dict[str, Any]]:
        if requirements is None:
            return None
        if state.status != "awaiting_approval" or state.active_run_id is None:
            raise ValueError("Requirements can only resume an awaiting-approval task.")
        expected = {req["id"] for req in state.requirements}
        if (
            not expected
            or len(requirements) != len(expected)
            or {req.id for req in requirements} != expected
            or not all(req.is_resolved() for req in requirements)
        ):
            raise ValueError("Resolve exactly the stored requirements before continuing this run.")
        state.status = "running"
        state.requirements = [req.to_dict() for req in requirements]
        self._store._checkpoint(state, "approval_resolved", requirements=state.requirements)
        return {"run_id": state.active_run_id, "requirements": requirements}

    def _fail(self, state: TaskState, exc: BaseException) -> None:
        state.status = "failed" if isinstance(exc, Exception) else "interrupted"
        state.note = f"{type(exc).__name__}: {exc}"
        try:
            self._store._checkpoint(state, "runner_stopped", status=state.status, note=state.note)
        except Exception as store_error:
            log_warning(f"Could not persist runner failure: {store_error}")

    def run(
        self, task_id: str, *, recover: bool = False, additional_turns: int = 0,
        requirements: Optional[list[RunRequirement]] = None,
        on_event: Optional[Callable[[LoopEvent], None]] = None,
    ) -> TaskState:
        with self._ownership():
            state = self._prepare(task_id, recover, additional_turns)
            resume = self._resume_kwargs(state, requirements)
            try:
                while True:
                    prompt = None if resume else self._next_prompt(state)
                    if prompt is None and resume is None:
                        return state
                    output = None
                    options = dict(session_id=state.session_id, user_id=state.user_id, stream=True,
                                   stream_events=True, yield_run_output=True)
                    stream = (self.agent.continue_run(**resume, **options) if resume else self.agent.run(
                        prompt, run_id=state.active_run_id, output_schema=TurnDecision,
                        add_history_to_context=True, **options,
                    ))
                    try:
                        for item in stream:
                            if isinstance(item, RunOutput):
                                output = item
                            else:
                                self._observe(state, item, on_event)
                    finally:
                        stream.close()
                    self._finish(state, output)
                    self._notify(LoopEvent(task_id=task_id, kind="task_status", payload=state.model_dump()), on_event)
                    resume = None
            except BaseException as exc:
                self._fail(state, exc)
                raise

    async def arun(
        self, task_id: str, *, recover: bool = False, additional_turns: int = 0,
        requirements: Optional[list[RunRequirement]] = None,
        on_event: Optional[Callable[[LoopEvent], None]] = None,
    ) -> TaskState:
        # SQLite control transactions are short and synchronous; model/tool I/O uses native Agno async APIs.
        with self._ownership():
            state = self._prepare(task_id, recover, additional_turns)
            resume = self._resume_kwargs(state, requirements)
            try:
                while True:
                    prompt = None if resume else self._next_prompt(state)
                    if prompt is None and resume is None:
                        return state
                    output = None
                    options = dict(session_id=state.session_id, user_id=state.user_id, stream=True,
                                   stream_events=True, yield_run_output=True)
                    stream = (self.agent.acontinue_run(**resume, **options) if resume else self.agent.arun(
                        prompt, run_id=state.active_run_id, output_schema=TurnDecision,
                        add_history_to_context=True, **options,
                    ))
                    try:
                        async for item in stream:
                            if isinstance(item, RunOutput):
                                output = item
                            else:
                                self._observe(state, item, on_event)
                    finally:
                        await stream.aclose()
                    self._finish(state, output)
                    self._notify(LoopEvent(task_id=task_id, kind="task_status", payload=state.model_dump()), on_event)
                    resume = None
            except BaseException as exc:
                self._fail(state, exc)
                raise
