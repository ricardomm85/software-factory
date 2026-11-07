# Plan — Personal Software Factory

**Date:** 2026-11-07 · **Status:** Phases 0–5 closed — v1 shipped and observable; remaining: Phase 6 (scale, v2) and the polish items in §9 · **Workspace:** `~/Projects/software-factory`

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
| 4 | **Review + measurement** | multi-lens reviewer fan-out (see design note below), revise loop, `quality.py` with real commands (no placeholders), `run.finish(accepted=)`, tokens/cost per run in the trace, home-grown eval (5–10 known tasks from your repos) | `factory simple-sdlc` + cost/success scorecard per run |
| 5 | **Observability product** | read-only UI over SQLite (per-phase waterfall), optional human checkpoints pre-commit | Watch a full run live without reading transcripts |
| 6 | **Scale (v2)** | parallelism, branch-per-run + Docker sandbox, GitHub triggers, risk-based checkpoints, self-improvement, VPS if needed | Runs in parallel without direct supervision |

Every phase ends in something **runnable**, not something designed.

### Phase 4 design note — multi-lens code review (added 2026-11-07)

The manual `/code-review` run that validated Phase 1 maps 1:1 onto the factory, so Phase 4's
reviewer becomes a **fan-out of bounded review agents** instead of a single reviewer:

- **Parallel reviewer phases**, each with a typed envelope:
  `FindingsOutput { issues: [{file, line, description, reason}] }`.
- **A scoring phase** (verifies each finding against the code with a 0–100 rubric).
- **The ≥50 threshold is a gate** — deterministic code, not an agent: parse scores, filter,
  fail the phase while any verified finding is unfixed. The fix loop for what crosses the
  threshold is the Phase 2 bounded correction loop.
- **Roster effort per lens** (decided by Ricardo): context lenses (eligibility, instruction
  files, change summary) `low` · conventions reviewer `medium` · bug reviewers (shallow scan
  + git history) `high` · scorers `medium` · fixer `medium`.
- **Cost lesson from the manual run** (~3M subagent tokens at high effort with unbounded
  prompts): the deep bug lenses are where reasoning pays — the two grave Phase 1 findings
  (stderr deadlock, tracer threading) only surfaced there — while everything else runs
  cheaper. Bounded prompts + per-role effort keep the factory version a fraction of the cost.

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
(openai-completions). Keys live in the repo-local `.env` (gitignored, chmod 600,
documented by the committed `.env.sample` — the sssf convention). No global fallback:
one repo, one key set — explicit over implicit. Pi never sees the key in config
files or argv. The plan serves the GLM-5.x family
(`glm-5.2` offers 1M context if a stronger planner is needed later).
**Roster decision (2026-11-07): run every agent on `zai/glm-5.3-flash` for now** — phase
differentiation comes from `thinking` levels, not model ids. Revisit per-role models once
there is real cost/latency data per phase.

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
loader + validator, `agent_pi.py` pi spawner with subprocess env injection, `env.py`
repo-local secret loader, `__main__.py` CLI `factory "prompt" [--agent NAME]
[--mode print|json]`), the starter roster in `config.yaml` (scout / planner / builder —
all on `zai/glm-5.3-flash`; planner keeps `thinking: high`, the rest inherit `medium`),
`pyproject.toml` (uv-managed; pyyaml runtime, pytest dev), and 16 passing unit tests. Live smoke:
`uv run factory "<prompt>" --agent scout` spawned a real `zai/glm-5.3-flash` run that
inspected the repo and answered. All agents run `zai/glm-5.3-flash` (planner keeps
`thinking: high`; the rest inherit the `medium` default). The SQLite schema originally
listed under Phase 0 scope
moves to Phase 1 — it belongs with the tracer that defines its tables.
**Next: Phase 1 (spine)** — `run` with id, `run.phase()` context manager, pydantic envelope
with same-session retry, events → SQLite WAL, CLI commands `sessions`/`phases`.

**Phase 1 — CLOSED (2026-11-07).** Shipped: `factory/db.py` (SQLite WAL trace — `sessions`,
`phases`, `events`, `envelopes` tables; cross-thread writes enabled for the streaming
tracer), `factory/envelope.py` (pydantic `EnvelopeBase` / `RunOutput`, tolerant JSON
extraction, the `## Report` contract rendered into every agent prompt), `factory/run.py`
(`Run` + `run.phase()` context manager born `fail`; `call_agent` acceptance loop with
same-session corrections via `--session-id {run_id}-{agent}` — never cold-restart),
`factory/agent_pi.py::run_pi_streaming` (JSONL events piped live to the tracer from a pump
thread), and the CLI: `factory run | sessions | phases | prompt`. A crashed or killed run
settles its own trace to `failed`.
Live verification: two runs accepted end-to-end (`105f2e60`, `ef4e28ef`); trace queryable
via `factory sessions`, `factory phases <id>`, and direct SQLite (108 `pi_event` rows on
the second run). 36 unit tests passing.
**Next: Phase 2 (gates)** — `gates.py` (`artifacts_exist`, `diff_matches_claims`,
`tests_pass`), the bounded correction loop wired to gate violations, and the `writes`
boundary with rollback. Deliverable: `factory build-test "<prompt>"`.

**Phase 2 — CLOSED (2026-11-07).** Shipped: `factory/gates.py` (post-hoc gates with one
`Check` per thing examined — `run_output_gate`, `artifacts_exist`, `diff_matches_claims`
with rollback of unclaimed changes: tracked files restored, untracked files removed —
declared-but-unchanged stays a note, not a failure), `factory/chains.py::run_build_test`
(bounded build→test rounds; gate violations and red suites corrected in the SAME builder
session via `prompts/gate_correction.md` / `prompts/test_correction.md`; the run settles
its own trace on any crash), `BuildOutput` envelope (`changed_files` is what the diff gate
verifies; `commit_message` proposed, never executed by the agent), `defaults.test_command`
in the roster (the real suite — placeholders are theater), and the CLI:
`factory build-test "<prompt>"` requiring a clean working tree as the writes-boundary
baseline.
Live verification (dogfood): run `a97f272b` accepted — builder wrote `PHASE2.md` claiming
it in `changed_files`, gates verified it against the real `git status`, pytest ran green
(1s), trace shows both lanes (`build:r1` agent 19s / `test:r1` code 1s) with
`gate_results` + `test_run` events. 52 unit tests passing.
**Next: Phase 3 (standard chain)** — planner + inter-agent handoff (`context_handoff/`,
envelope injected into the next prompt), commit phase owned by code, multi-model roster
options. Deliverable: `factory plan-build-test "<prompt>"`.

**Phase 3 — CLOSED (2026-11-07).** Shipped: `factory/chains.py::run_plan_build_test`
(plan → handoff → commit:plan → gated build→test rounds → code-owned commit; the plan
crosses agents as a file whose content is injected into the builder's prompt — context
transfers in code, not conversation; two author-flavored commits when green: the plan,
then the implementation carrying the builder's proposed message), the `PlanOutput`
envelope (`plan_path` + `artifacts`, verified by the artifacts gate),
`prompts/plan_instruction.md`, and the CLI `factory plan-build-test`
(`--planner/--builder/--test-command/--max-rounds`; shares the clean-tree precondition
and test-command resolution with build-test).
**Milestone: first self-hosted commit.** Run `ecd4459c` accepted on this very repo: the
planner investigated and wrote `specs/ecd4459c-plan.md`, gates failed rounds 1–2 (unclaimed
changes rolled back, corrected in the same session), round 3 went green, pytest passed, and
the chain landed `6fcd1c6` (plan) + `13ac23c` (implementation) by itself. 60 unit tests
passing.
**Next: Phase 4 (multi-lens review)** — per the design note in §5: reviewer fan-out with
typed findings envelopes, a scoring phase, and the ≥50 threshold as a deterministic gate.

**Phase 4 — CLOSED (2026-11-07).** Shipped: `factory/chains.py::run_build_review` (multi-lens
review of the working-tree changes: per-lens reviewer phases → merged findings → scorer phase
→ deterministic gate that blocks on any finding scored ≥50 → bounded fix rounds with a fixer
that re-claims its changes; the user's own WIP at chain start is exempt from the fixer's
rollback), `FindingsOutput` / `ScoredOutput` envelopes (`Finding{file, line, description,
reason}` + `score`), reviewer/scorer/fix prompts as data (`prompts/review_{lens}.md`,
`score_findings.md`, `fix_findings.md` — the scorer's rubric is the validated 0–100 scale),
roster lenses per the §5 effort decisions (`review-conventions` medium, `review-bugs` high,
`scorer` medium; the builder doubles as fixer), and the CLI `factory build-review`
(`--lenses/--scorer/--fixer/--max-rounds/--timeout` — reviews the working tree, so WIP is
the natural input).
Live verification (self-review dogfood): a deliberate Spanish-text violation planted as WIP
was caught by the lens reviewers, scored ≥50 by the scorer, blocked by the gate, FIXED by
the fixer in-session (file rewritten to compliant English), and the round-2 re-review came
back clean — run `c7ec442f`, phases `review:*:r1 → score → gate → fix → review:*:r2 → score
→ gate`. Also surfaced and fixed two transport bugs en vivo (per-invocation timeout is now
configurable; the first attempt timed out at 300s). 68 unit tests passing.
**v1 core is complete:** the factory now plans, builds, tests, reviews, and fixes itself.
Natural next steps, in no fixed order: `context_handoff/` as first-class handoff storage,
parallel reviewer execution (Phase 6's fleet), and the roster-vs-target-repo coupling
decision noted in the Phase 2 review.

### Phase 5 closure note — observability UI (added 2026-11-07)

`factory/ui.py` + `factory ui --port`: a stdlib-only read-only web page over the trace —
no new dependencies, no external assets, loopback binding only. Routes: `/` (recent runs
with status, duration, phase/event counts) and `/run/{id}` (request summary, per-phase
waterfall with proportional duration bars, orchestrator-event timeline, pi stream shown
as collapsed counts). The run page auto-refreshes every 5s while the run is still
`running`: watching a run live requires no transcripts.

Two properties are enforced by SQLite, not by convention: every request opens the db on a
`query_only` connection (any accidental write raises), and WAL means the page renders
while the factory is mid-write (covered by a test that holds an uncommitted writer
transaction and fetches the index). All agent-produced text is HTML-escaped; long
payloads collapse behind `<details>`. 15 new tests (83 total). Live verification against
the real trace: index and run pages render Phases 1–4 history correctly
(run `c7ec442f`: waterfall, accepted badge, pi counts).

Dogfood: the factory reviewed its own UI diff with `factory build-review` —
"no findings scored >=50". The deferred half of Phase 5 (human checkpoints pre-commit)
stays on the §9 list.

### Phase 5b closure note — human checkpoints + a live-surfaced trace bug (added 2026-11-07)

`factory plan-build-test --checkpoint` (or `defaults.checkpoint: true` in the roster;
`--no-checkpoint` overrides): before the final commit lands, a `checkpoint` phase
(owner `human`) shows the pending changes (status + diffstat) and asks
`commit these changes? [y/N]` at stdin. Declining raises `CheckpointDeclined` — the run
settles rejected and the working tree is left uncommitted for the human to dispose of.
No TTY attached refuses too: a checkpoint that silently approves itself would be theater.
Request and decision are both in the trace. 89 tests passing (5 checkpoint + 1 boundary).

The live smoke then surfaced a real factory bug: in a target repo without a
`.gitignore`, the writes boundary's rollback (`git clean`) treated `data/factory.db`
as agent debris and deleted the trace mid-run — the open connection kept writing to an
unlinked inode and the trace was lost silently (it had survived until now only because
`data/` is gitignored in this repo). Fix: `_trace_exemptions()` exempts the trace db
plus its WAL sidecars from every gate rollback, from both commits (`git reset -q` after
`add -A`), and from the reviewer diff collector. Regression test: the whole chain runs
in an ignore-free repo and a fresh connection reads the full trace while no commit
carries `data/`. Verified live: the no-TTY smoke leaves run `e0e4a6f4` with all 7 phases
recorded (844 pi events) and the plan commit carries only `specs/<run>-plan.md`.

## References

- Video: https://www.youtube.com/watch?v=haUfb1ievTE (IndyDevDan)
- sssf: https://github.com/disler/super-simple-software-factory (the `example` branch has the factory stamped into a demo repo)
- Warp Factories: https://try.warp.dev/software-factories/software-factory-v2 · Factory.com: https://factory.com
- Huntley: https://ghuntley.com/rad · MetaGPT: https://github.com/foundationagents/metagpt
- Z.AI × Claude Code: https://docs.z.ai/devpack/tool/claude · OpenCode CLI: https://opencode.ai/docs/cli
