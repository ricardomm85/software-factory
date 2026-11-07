"""The spine: runs, phases, and the acceptance loop.

Every phase is born `fail`; only a clean exit flips it (gates arrive in Phase 2 —
for now, envelope validation is the gate). A bad agent reply is corrected inside
the same pi session: a correction costs one message, a cold restart throws away
everything the agent learned.
"""

import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ValidationError

from .agent_pi import AgentResult, run_pi_streaming
from .db import connect
from .envelope import RunOutput, parse_envelope, report_contract


class AgentPhaseError(Exception):
    """Raised when an agent phase cannot produce a valid, successful envelope."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_id() -> str:
    return uuid.uuid4().hex[:8]


class Phase:
    """Handle yielded inside a ``run.phase()`` block. Logging only — nothing here decides."""

    def __init__(self, run: "Run", phase_id: int, name: str, kind: str, owner: str):
        self.run = run
        self.id = phase_id
        self.name = name
        self.kind = kind
        self.owner = owner

    def log(self, type: str, payload: dict) -> None:
        self.run.log_event(self.id, type, payload)


class Run:
    """One traced run: a row in sessions, phases under it, events streaming in."""

    def __init__(self, conn, run_id: str):
        self.conn = conn
        self.id = run_id

    @classmethod
    def create(cls, conn, request: str, agent: str, model: str) -> "Run":
        run_id = new_run_id()
        conn.execute(
            "INSERT INTO sessions (id, request, status, agent, model, started_at)"
            " VALUES (?, ?, 'running', ?, ?, ?)",
            (run_id, request, agent, model, _now()),
        )
        conn.commit()
        return cls(conn, run_id)

    @classmethod
    def get(cls, conn, run_id: str) -> "Run":
        row = conn.execute("SELECT id FROM sessions WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown run id: {run_id}")
        return cls(conn, run_id)

    def log_event(self, phase_id: int | None, type: str, payload: dict) -> None:
        self.conn.execute(
            "INSERT INTO events (run_id, phase_id, type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (self.id, phase_id, type, json_dumps(payload), _now()),
        )
        self.conn.commit()

    def finish(self, accepted: bool, reason: str = "") -> None:
        """Settle the run: phases passing is not the same as the run being acceptable."""
        status = "accepted" if accepted else "failed"
        self.conn.execute(
            "UPDATE sessions SET status = ?, ended_at = ? WHERE id = ?",
            (status, _now(), self.id),
        )
        if reason:
            self.log_event(None, "run_rejected", {"reason": reason} if not accepted else {"note": reason})
        self.conn.commit()

    @contextmanager
    def phase(self, name: str, kind: str = "agent", owner: str = "code"):
        """Context manager for one phase. Born fail; a clean exit flips it to pass."""
        seq = self.conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM phases WHERE run_id = ?", (self.id,)).fetchone()[0]
        cur = self.conn.execute(
            "INSERT INTO phases (run_id, seq, name, kind, owner, status, started_at)"
            " VALUES (?, ?, ?, ?, ?, 'fail', ?)",
            (self.id, seq, name, kind, owner, _now()),
        )
        self.conn.commit()
        handle = Phase(self, cur.lastrowid, name, kind, owner)
        try:
            yield handle
        except Exception as exc:
            self.conn.execute(
                "UPDATE phases SET status = 'fail', ended_at = ?, error = ? WHERE id = ?",
                (_now(), f"{type(exc).__name__}: {exc}", cur.lastrowid),
            )
            self.conn.commit()
            raise
        self.conn.execute(
            "UPDATE phases SET status = 'pass', ended_at = ? WHERE id = ?",
            (_now(), cur.lastrowid),
        )
        self.conn.commit()

    def phases(self) -> list:
        return self.conn.execute(
            "SELECT * FROM phases WHERE run_id = ? ORDER BY seq", (self.id,)
        ).fetchall()


def json_dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def call_agent(
    run: Run,
    phase: Phase,
    prompt: str,
    agent_cfg: dict,
    output_type: type[BaseModel] = RunOutput,
    max_attempts: int = 3,
    agent_fn=run_pi_streaming,
    repo_dir: Path | None = None,
) -> BaseModel:
    """Run one agent phase: prompt in, typed envelope out, same-session retries.

    The pi session id is ``{run_id}-{agent}`` so later calls to the same agent in
    the same run resume its context window instead of starting cold.
    """
    session_id = f"{run.id}-{agent_cfg['name']}"
    full_prompt = f"{prompt}\n\n{report_contract(output_type)}"
    last_text = ""

    for attempt in range(1, max_attempts + 1):
        phase.log("attempt", {"attempt": attempt, "session_id": session_id})
        prompt_for_attempt = full_prompt
        if attempt > 1:
            prompt_for_attempt = (
                f"Your previous reply was not a valid envelope: {last_error}. "
                f"Reply again with ONLY the JSON object matching the ## Report contract. "
                f"Nothing else."
            )
            phase.log("correction", {"attempt": attempt, "error": last_error})

        result: AgentResult = agent_fn(
            prompt=prompt_for_attempt,
            model=agent_cfg["model"],
            session_id=session_id,
            thinking=agent_cfg.get("thinking"),
            on_event=lambda evt: phase.log("pi_event", evt),
            repo_dir=repo_dir,
        )
        last_text = result.stdout

        try:
            envelope = parse_envelope(last_text, output_type)
        except (ValueError, ValidationError) as exc:
            last_error = str(exc)
            _store_envelope(run.conn, phase.id, attempt, valid=False, data=last_text)
            phase.log("envelope_invalid", {"attempt": attempt, "error": last_error})
            continue

        _store_envelope(run.conn, phase.id, attempt, valid=True, data=last_text)
        phase.log("envelope_ok", {"attempt": attempt, "status": envelope.status})
        if envelope.status != "success":
            # A parsed envelope that reports failure: the phase ran fine, the run
            # will not be accepted. Do not retry — the agent decided.
            return envelope
        return envelope

    raise AgentPhaseError(
        f"agent {agent_cfg['name']!r} produced no valid envelope after {max_attempts} attempts "
        f"(last error: {last_error})"
    )


def _store_envelope(conn, phase_id: int, attempt: int, valid: bool, data: str) -> None:
    conn.execute(
        "INSERT INTO envelopes (phase_id, attempt, valid, data, created_at) VALUES (?, ?, ?, ?, ?)",
        (phase_id, attempt, int(valid), data, _now()),
    )
    conn.commit()
