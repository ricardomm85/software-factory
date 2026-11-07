# Plan — Personal Software Factory

**Date:** 2026-11-07 · **Status:** planned, no code yet · **Workspace:** `~/Projects/software-factory`

## 1. Goal

A personal factory to ship **features in my own repos** through agent chains
(plan → build → test → review), where **the Python code owns the loop** and agents are
bounded nodes inside it. Governing principle: *"Agent proposes, code disposes."*

## 2. Decisions made

| Decision | Choice | Why |
|---|---|---|
| Initial use case | Features in own repos | Shortest path to real value; defines which gates matter |
| Orchestrator stack | Python 3.12+, `uv`, pydantic, SQLite | It's I/O-bound; it's what agents write best → fast iteration |
| Hosting | Local first; VPS only when a real need arrives (24/7, webhooks) | Zero cost, immediate iteration; portable design (SQLite state, runs by id) |
| Starting point | From scratch, copying sssf's **shape** (not its code) | Full ownership, fits the use case from day 1 |
| Agent backend | Harness abstracted in config (`coding_agent:`) — concrete decision in Phase 0 | Switching CLI/provider must be config, not code |
| Language | All docs and code in English (see [AGENTS.md](AGENTS.md)); conversation with the owner in Spanish | Repo artifacts stay in-distribution for agents and shareable |

## 3. The shape (what we copy from sssf)

- **Phases as context managers**, three lanes: `engineer` (human), `agent` (`ph.call(...)`),
  `code` (deterministic step). Every phase is born `fail`; clean exit + green gates approve it.
- **Typed envelopes** (pydantic): `status`, `summary`, `artifacts`, `notes_for_next_agent`
  plus per-agent fields. Context travels between agents in envelopes and `context_handoff/`,
  not in conversation.
- **Post-hoc gates**: verify the envelope's declarations after the fact (`artifacts_exist`,
  `diff_matches_claims`, `tests_pass`). On failure: **correction in the same session**
  (resume by id), never a cold restart.
- **Trace in SQLite (WAL)**: one cursor query is the whole transport. No WebSockets, no ingest.
- **YAML roster** (who each agent is: model, thinking level, prompts, tools, `writes`) kept
  separate from call sites (how it's used). ADWs never name a model, they name an agent.
  Model and effort are chosen **per agent (role/phase)** in the roster; per-task choice is done
  by picking a different agent variant (e.g. `builder-fast` vs `builder-strong`) at the call
  site — never by passing a loose model. Ad-hoc model overrides are allowed but must be
  recorded in the run's trace: a run that cannot be reproduced from its trace is broken.
- **`writes` is the boundary**, not `tools`: working-tree comparison before/after every call;
  unauthorized change → rollback + phase fails.

## 4. Scope v1 / v2

Consolidated from the landscape (Warp, Factory.com, Huntley, MetaGPT, sssf):

- **v1:** A. execution spine · B. contracts (envelopes/gates) · C. basic observability ·
  D. config-as-code · **+ basic I**: cost per run in the trace and a small home-grown eval.
- **v2 (when it hurts):** E. fleet/parallelism · F. human checkpoints by risk ·
  G. triggers (webhooks/issues) · H. branch-per-run + sandbox · advanced I (self-improvement).

## 5. Phases

| # | Phase | Scope | Deliverable (definition of done) |
|---|---|---|---|
| 0 | **Spike** | Pick the agent CLI + provider (see §6). Repo skeleton: `factory/` (package), `config.yaml`, SQLite schema | A Python script spawns the agent headless with a prompt and captures structured output |
| 1 | **Spine** | `run` with id, `run.phase()` context manager, pydantic envelope with same-session retry, events → SQLite WAL, CLI commands `sessions`/`phases` | `factory run "<prompt>"` — 1 agent, parsed envelope, queryable trace |
| 2 | **Gates** | `gates.py`, bounded correction loop (max N rounds), `writes` boundary with rollback | `factory build-test "<prompt>"` — builder + code(test) + fix loop. Already genuinely useful |
| 3 | **Standard chain** | planner + inter-agent handoff, commit phase owned by code (builder proposes the message), YAML roster with multiple models | `factory plan-build-test` — real features end-to-end. **Milestone: first self-hosted commit** — the factory ships a non-core change to this very repo (see §7) |
| 4 | **Review + measurement** | reviewer + revise loop, `quality.py` with real commands (no placeholders), `run.finish(accepted=)`, tokens/cost per run in the trace, home-grown eval (5–10 known tasks from your repos) | `factory simple-sdlc` + cost/success scorecard per run |
| 5 | **Observability product** | read-only UI over SQLite (per-phase waterfall), optional human checkpoints pre-commit | Watch a full run live without reading transcripts |
| 6 | **Scale (v2)** | parallelism, branch-per-run + Docker sandbox, GitHub triggers, risk-based checkpoints, self-improvement, VPS if needed | Runs in parallel without direct supervision |

Every phase ends in something **runnable**, not something designed.

## 6. Agent backend and cost (Phase 0 decision)

**To start: the Z.AI GLM subscription only.** OpenRouter is deferred to the end
(decision 2026-11-07): one flat quota, one provider first; the multi-provider roster
arrives in v2 if needed.

pi connects to Z.AI via `~/.pi/agent/models.json` — a custom provider with Z.AI's `baseUrl`
and the plan's API key (openai-completions protocol, or the Anthropic-compatible endpoint
`https://api.z.ai/api/anthropic`). The roster still differentiates models **within** z.ai
(strong GLM for planner/reviewer, fast GLM for builder): multi-model ≠ multi-provider.
Documented plan B: the `claude` CLI with the same endpoint; PTY terminal automation is
discarded for v1 (fragile, no structured output).

> Note: the "leave a terminal open and drive it" idea is essentially what **resume by
> session-id** already gives you, robustly: persistent context with no screen scraping.
> The PTY variant stays as a fallback if some CLI didn't support headless mode.

**Spike criteria** (all three must pass): ① headless with structured output
(`claude -p --output-format json|stream-json` / `opencode run`), ② session resume by id
(corrections without cold restart), ③ line-by-line events (JSONL) for the live trace.
The winner is declared in `config.yaml` as `coding_agent:` — never hardcoded.

**Spike result (2026-11-07): pi CONFIRMED** — all three criteria pass with `zai/glm-5.3-flash`:
① `pi -p` one-shot returns clean parseable JSON on stdout (exit 0);
② `--session-id <id>` is create-or-continue — context recalled across two separate invocations;
③ `--mode json` streams typed JSONL events (`session`, `agent_start`, `message_*`,
`tool_execution_*`, `turn_*`, `agent_end`) — the exact feed the SQLite tracer will consume.
Working setup: provider `zai` in `~/.pi/agent/models.json` → `https://api.z.ai/api/paas/v4`
(openai-completions), key interpolated from `~/.config/factory/.env` (`ZAI_API_KEY`, 600 perms,
outside any repo; pi never sees the key in config). The plan serves the GLM-5.x family:
`glm-5.2` (1M context) → planner/reviewer candidate; `glm-5.3-flash` (fast) → builder
candidate; also `glm-5.3`, `glm-5.3-flashx`, `glm-4.7`, `glm-5-turbo`. `~/.pi/` is machine
config, never committed; the factory repo will stamp its own copy for other machines.

**Candidates evaluated (2026-11-07):** current recommendation = **pi** (native orchestration,
minimal, frictionless Z.AI; [models.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/models.md)).
Evaluated and discarded for v1:
- `mini-swe-agent` (>74% SWE-bench Verified with bash-only and ~100 lines) → design reference;
  in-process candidate for Phase 6. https://github.com/swe-agent/mini-swe-agent
- `Kimi Code` (Moonshot) → model-locked; Kimi enters as a *model* via OpenRouter in the roster,
  not as a CLI. https://www.kimi.com/en/help/kimi-code/cli-getting-started
- `oh-my-pi (omp)` (pi fork with LSP, subagents, 32 tools) → **upgrade path for Phase 3+**,
  same orchestration contract. https://github.com/can1357/oh-my-pi
- Public benchmarks (Terminal-Bench 2.0/2.1, SWE-bench, Strands' harness study with ~94k trials):
  the model dominates, the harness is second order. The real benchmark = home-grown eval over
  tasks from Ricardo's repos (Phase 4).

## 7. Self-hosting (dogfooding)

This repo is itself a target repo for the factory. From Phase 3 on, the factory iterates on itself.

| Stage | How the factory iterates on itself |
|---|---|
| Phases 0–2 | Bootstrap with the usual tools (ZCode, agent CLIs) — there is nothing to self-host yet |
| Phase 3+ | The factory ships non-core changes to this repo (prompts, docs, CLI, UI) |
| Phase 4+ | The home-grown eval set = this repo's own history; improvements are measured with its own metrics |
| Always | Human review of the orchestrator core; `protected_files` enforced |

Rules specific to self-hosting:

- **`protected_files` counts double here**: an agent working on the factory must not rewrite
  the machinery that grades it (`factory/` core, `gates.py`). That is sssf's rule already;
  for this repo it is critical.
- **The factory never merges into its own core unreviewed**: core edits require a human
  checkpoint — and, until v2's branch-per-run exists, at least a clean tree plus a manual
  branch before the run starts.
- **Infinite regress is real**: a bug in the orchestrator can break the runs trying to fix it.
  If the factory is on fire, core fixes go back to the usual tools.
- **Shape decisions stay human**: adding a phase, changing the envelope contract, swapping
  harnesses — the factory implements; it does not decide its own architecture.

## 8. Honest risks (lessons from sssf and the landscape)

- `quality.py` with placeholder commands that exit 0 = theater. **The first thing to
  customize in Phase 4 is your real commands.**
- The output-contract triangle: the type in `data_types.py` + the `## Report` example in that
  agent's `user.md` + `output_type=` at the call site. Change one = change all three in the
  same edit.
- v1 runs on the current working tree: no sandbox, no branch-per-run. Cheap mitigation:
  require a clean tree when a run starts + always commit at the end of a successful chain.
- The GLM plan quota may limit future parallelism (v2). Relief valves in cost order:
  lower `thinking`, faster GLM models within z.ai, and OpenRouter only at the end.

## 9. Next step

**Phase 0 — CLOSED (2026-11-07).** Shipped: the `factory/` package (`config.py` roster
loader + validator, `agent_pi.py` pi spawner with subprocess env injection, `env.py` secret
loader for `~/.config/factory/.env`, `__main__.py` CLI `factory "prompt" [--agent NAME]
[--mode print|json]`), the starter roster in `config.yaml` (scout / planner / builder —
planner on `zai/glm-5.2` high thinking, the rest on `zai/glm-5.3-flash`), `pyproject.toml`
(uv-managed; pyyaml runtime, pytest dev), and 10 passing unit tests. Live smoke:
`uv run factory "<prompt>" --agent scout` spawned a real `zai/glm-5.3-flash` run that
inspected the repo and answered. The SQLite schema originally listed under Phase 0 scope
moves to Phase 1 — it belongs with the tracer that defines its tables.
**Next: Phase 1 (spine)** — `run` with id, `run.phase()` context manager, pydantic envelope
with same-session retry, events → SQLite WAL, CLI commands `sessions`/`phases`.
Deliverable: `factory run "<prompt>"` with a queryable trace.

## References

- Video: https://www.youtube.com/watch?v=haUfb1ievTE (IndyDevDan)
- sssf: https://github.com/disler/super-simple-software-factory (the `example` branch has the factory stamped into a demo repo)
- Warp Factories: https://try.warp.dev/software-factories/software-factory-v2 · Factory.com: https://factory.com
- Huntley: https://ghuntley.com/rad · MetaGPT: https://github.com/foundationagents/metagpt
- Z.AI × Claude Code: https://docs.z.ai/devpack/tool/claude · OpenCode CLI: https://opencode.ai/docs/cli
