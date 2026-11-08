# Plan — Personal Software Factory

**Date:** 2026-11-08 · **Status:** Phases 0–8 closed — the factory is a product: an always-on board at `factory serve` that plans, builds, tests, reviews, asks, and ships PRs. Remaining: Phase 9 product surface (onboarding + repo-first, decided 2026-11-08) · Phase 10 scale (when it hurts) · **Workspace:** `~/Projects/software-factory`

## 1. Goal

A personal factory to ship **features in my own repos** through agent chains
(plan → build → test → review), where **the Python code owns the loop** and agents are
bounded nodes inside it. Governing principle: *"Agent proposes, code disposes."*

Since the 2026-11-07 pivot the primary interface is the **local web board**: create a
task, watch the card move across the kanban, and answer when the factory parks it to
ask. The CLI keeps running the same chains underneath — it becomes the engine's debug
surface, not the owner's desk.

## 2. Decisions made

| Decision | Choice | Why |
|---|---|---|
| Initial use case | Features in own repos | Shortest path to real value; defines which gates matter |
| Orchestrator stack | Python 3.12+, `uv`, pydantic, SQLite | It's I/O-bound; it's what agents write best → fast iteration |
| Hosting | Local first; VPS only when a real need arrives (24/7, webhooks) | Zero cost, immediate iteration; portable design (SQLite state, runs by id) |
| Starting point | From scratch, copying sssf's **shape** (not its code) | Full ownership, fits the use case from day 1 |
| Agent backend | Harness abstracted in config (`coding_agent:`) — concrete decision in Phase 0 | Switching CLI/provider must be config, not code |
| Language | All docs and code in English (see [AGENTS.md](AGENTS.md)); conversation with the owner in Spanish | Repo artifacts stay in-distribution for agents and shareable |
| Interface (pivot 2026-11-07) | Web board first (kanban at localhost), CLI kept as debug surface | The owner directs work from the board; watching and answering beats terminal flags |
| Board shape | Columns **Backlog / Working / In PR** plus a separate **Archived** section; pauses show as a waiting badge on the Working card | The board stays calm; "someone has to answer this" is a badge, not a fifth column |
| Concurrency | One active run per repo; per-repo FIFO queue; repos in parallel | Runs on the same repo would step on each other's git state |
| Delivery | PR on GitHub (branch per task → push → `gh pr create`); card lands in In PR, moves to Archived on merge | Reviewable, revertible, and matches how the owner ships |
| Deployment | Always-on home PC, one user, many repos; access from other devices via SSH tunnel or Tailscale, loopback binding stays | Standard ops (systemd user service); no auth to build while the port never leaves loopback |
| Run cap | Global cap on simultaneous runs (config), sized to RAM | One-per-repo prevents git collisions, not RAM collisions — the target repos' Chrome E2E suites are the heavy part |
| Product surface (2026-11-08) | Repo-first navigation; repos discovered from GitHub with clone-on-register; first-run setup UI for tokens | The owner's mental model: enter → see my repos → enter one → see its tasks; onboarding WRITES the same config-as-data files, it does not replace them |
| Visual surface (2026-11-08) | `factory/web/` package + real `static/` assets, one dark theme, JS fragment polling (no reloads), cards name the live phase, run page as a plain-English timeline, archived as a searchable table | The owner's verdict on the working board was "no me entero de nada" — the factory must read at a glance; structure decided with the owner (see the Phase 9 "board's face" design note) |

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
- **v2 (redefined 2026-11-07):** the **web board** is the new v2 core — J. task board +
  execution daemon · K. web-mediated human gates (plan questions, plan approval, PR
  approval) · L. PR delivery + system-discovered cards. Two old v2 items arrive inside
  it: branch-per-run (as branch-per-task with PR delivery) and human checkpoints (as
  board pauses). Still deferred until it hurts: Docker sandbox · external triggers
  (webhooks/issues) · multi-run parallelism per repo · advanced self-improvement.

## 5. Phases

| # | Phase | Scope | Deliverable (definition of done) |
|---|---|---|---|
| 0 | **Spike** | Pick the agent CLI + provider (see §6). Repo skeleton: `factory/` (package), `config.yaml`, SQLite schema | A Python script spawns the agent headless with a prompt and captures structured output |
| 1 | **Spine** | `run` with id, `run.phase()` context manager, pydantic envelope with same-session retry, events → SQLite WAL, CLI commands `sessions`/`phases` | `factory run "<prompt>"` — 1 agent, parsed envelope, queryable trace |
| 2 | **Gates** | `gates.py`, bounded correction loop (max N rounds), `writes` boundary with rollback | `factory build-test "<prompt>"` — builder + code(test) + fix loop. Already genuinely useful |
| 3 | **Standard chain** | planner + inter-agent handoff, commit phase owned by code (builder proposes the message), YAML roster with multiple models | `factory plan-build-test` — real features end-to-end. **Milestone: first self-hosted commit** — the factory ships a non-core change to this very repo (see §7) |
| 4 | **Review + measurement** | multi-lens reviewer fan-out (see design note below), revise loop, `quality.py` with real commands (no placeholders), `run.finish(accepted=)`, tokens/cost per run in the trace, home-grown eval (5–10 known tasks from your repos) | `factory simple-sdlc` + cost/success scorecard per run |
| 5 | **Observability product** | read-only UI over SQLite (per-phase waterfall), optional human checkpoints pre-commit | Watch a full run live without reading transcripts |
| 6 | **Task board** | `tasks` table (cards: title, prompt, repo, origin owner\|system, status, latest run), roster gains a `repos:` list (path + per-repo test command — the create form offers repos by name, not raw paths), `factory serve` daemon owning execution (stdlib threads; one active run per repo, FIFO per repo, global cap on simultaneous runs), create-task form, kanban view — columns **Backlog / Working / In PR** + separate **Archived** section, card detail = the existing run page | Create a task in the browser and watch it execute without a terminal |
| 7 | **Web human gates** | `pauses` table (type `questions` \| `plan_approval` \| `pr_approval`; request, answer, status); plan envelope gains `questions` — the card parks with a waiting badge, you answer from the board, the same session resumes; plan approval before build; PR approval before push; CLI keeps the stdin checkpoint | A task cannot pass plan, build-start, or push without a board decision |
| 8 | **PR delivery + discovered work** | branch per task off the default branch, commit, push, `gh pr create`, PR URL on the card (column In PR; Archived on merge); envelopes gain `discovered` findings that must NOT enter this PR — deterministic code turns them into Backlog cards with `origin_run` provenance | Card lands in In PR with its URL; out-of-scope findings become new cards, never silent scope creep |
| 9 | **Product surface** | First-run `/setup` onboarding: Z.AI token validated with a real API call, GitHub token validated against the API and used for push/PR, clone base folder — writes `.env` (0600) and the `repos:` section; add-repo flow from `gh repo list` with existing-clone detection or clone-to-path; repo-first navigation (home = repo cards with per-column counts, repo page = its kanban + create form). The board's face shipped first (2026-11-08): `factory/web/` + static assets, dark theme, live fragment polling, run timeline, searchable archive | A fresh machine reaches a managed repo through the browser alone |
| 10 | **Scale, when it hurts** | Docker sandbox, external triggers (webhooks/issues), multi-run parallelism per repo, self-improvement, VPS | Unchanged principle: each item enters only when a real need arrives |

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

### Phase 6–8 design note — the board takes over (added 2026-11-07)

After watching v1 run from the terminal, the owner moved the desk to the browser:
create a task, watch the card move, answer when the factory parks it to ask. What
changes, concretely:

- **One long-lived `factory serve` owns execution.** The current `factory ui` grows
  into it: same stdlib server, same loopback-only binding, same run-detail page —
  plus the board and a create-task form. The server writes (task creation, pause
  resolution) through the same deterministic code paths the CLI uses; render
  connections stay `query_only`. The UI's read-only guarantee doesn't relax —
  it stops being the whole story.
- **Tasks are cards; runs are executions.** New `tasks` table (title, prompt, repo,
  origin: `owner|system`, status, latest run). Executing a task creates a run exactly
  as the CLI does today — the sessions schema is untouched. Card position derives from
  task state + open pauses: **Backlog** → **Working** (a waiting badge while a pause
  is open; a failed badge on a failed run, re-runnable) → **In PR** → **Archived**
  (on merge, or archived by the owner).
- **Pauses are rows, not callbacks.** New `pauses` table (run, phase, type:
  `questions` | `plan_approval` | `pr_approval`; request payload, answer, status).
  The chain's human-gate callback blocks on an event bound to the pause row in web
  mode; the CLI keeps the stdin callback. SQLite WAL remains the one transport —
  no WebSockets, polling stays.
- **Questions and plan approval extend the plan envelope.** `PlanOutput` gains
  `questions` — doubts raised before/while planning. The card parks in Working with
  the waiting badge; the owner answers from the board; answers re-enter the SAME pi
  session (resume by id, per the ground rules). Plan approval then shows the plan
  file — approve, or correct (same-session resume again). The stdin pre-commit
  checkpoint becomes the board's PR approval: see the diff, approve → push.
- **PR delivery.** Each task runs on its own branch off the repo's default branch;
  when the owner approves, code commits, pushes, and opens the PR (`gh pr create`);
  the card shows the PR URL and lands in In PR. This quietly delivers the old v2's
  branch-per-run for board tasks; CLI chains keep their local-commit behavior.
- **Discovered work is scope discipline, enforced by code.** Build/test/review
  envelopes gain `discovered: [{title, detail}]` — findings that must NOT enter the
  current PR. The fixer and gates may not absorb them; after the phase, deterministic
  code creates Backlog cards with `origin_run` provenance. The factory may propose
  work; only the owner promotes it.
- **Queue.** One active run per repo, FIFO within a repo, repos concurrent — runs on
  one repo would step on each other's git state. Queued tasks show their position.
  Stdlib threads suffice at this scale; the GLM quota relief valves (§8) keep their order.
- **RAM is guarded by the global run cap, not by the language.** The heavy part of a
  run is the target repo's own E2E suite (Chrome instances, ~0.5 GB each), not the
  orchestrator. A config cap on simultaneous runs, sized to the box's RAM, is the
  whole policy — one-per-repo prevents git collisions, the cap prevents memory ones.
- **Deployment (always-on home PC).** The daemon is meant to live on a dedicated
  home PC serving many repos: a systemd user service for boot survival and
  restart-on-crash, loopback binding kept, remote access from other devices via
  SSH tunnel or Tailscale — no auth to build while the port never leaves loopback.
  Crash recovery (below) is what makes an always-on daemon honest.
- **Crash recovery.** If the daemon dies mid-run, the trace may keep a `running` row
  no one owns. On startup the daemon reaps such orphans — settles them failed in the
  trace and parks their cards as failed, re-runnable by the owner. The trace stays
  the source of truth.

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
  checkpoint — and, until Phase 8's branch-per-task exists, at least a clean tree plus a
  manual branch before the run starts.
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

**Pivot (2026-11-07) — RESOLVED (2026-11-08): Phases 6, 7 and 8 shipped together in
one PR — see the board closure note above.** The board is now the primary interface:
tasks are created and directed from the local web board (columns **Backlog / Working /
In PR** plus a separate **Archived** section), the factory parks cards to ask (plan
questions, plan approval, PR approval), and delivery means an open PR. The old
Phase 6 (scale) is Phase 9.
**Decision (2026-11-08) — the board gets a front door.** Using the board live, the
owner restated the expected shape: enter the factory → see the repos I have access
to → enter one → see its pending tasks; first visit asks for the tokens and nothing
else. Decisions recorded in §2; design in the Phase 9 design note.
**Next: Phase 9 (product surface)** — `/setup` onboarding, add-repo from GitHub with
clone-on-register, repo-first navigation. The old scale phase is Phase 10.

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
Natural next steps, in no fixed order: `context_handoff/` as first-class handoff storage
and parallel reviewer execution (Phase 6's fleet). The roster-vs-target-repo coupling
decision is resolved — see the closure note below.

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
shipped in the Phase 5b note below.

### Phase 5b closure note — human checkpoints + a live-surfaced trace bug (added 2026-11-07)

`factory plan-build-test --checkpoint` (or `defaults.checkpoint: true` in the roster;
`--no-checkpoint` overrides): before the final commit lands, a `checkpoint` phase
(owner `human`) shows the pending changes (status + diffstat) and asks
`commit these changes? [y/N]` at stdin. Declining raises `CheckpointDeclined` — the run
settles as failed (the trace's rejection state is `failed`) and the working tree is left
uncommitted for the human to dispose of.
No TTY attached refuses too: a checkpoint that silently approves itself would be theater.
Request and decision are both in the trace. 93 tests passing (5 checkpoint + 1 boundary + 4
exemption-helper).

### Closure note — roster/target decoupling (added 2026-11-07)

`factory --repo <path>` resolves the roster-vs-target-repo coupling decision noted in the
Phase 2 review: one roster, one `.env`, one trace — any number of target repositories.
`repo_dir` (where agents work) and `env_dir` (where secrets and the roster live) are now
separate all the way down: `run_pi`/`run_pi_streaming` split cwd from `.env` loading,
`call_agent` forwards `env_dir`, the chains accept it, and the CLI resolves both from
`--repo` (top-level or per-subcommand). The trace gains a `sessions.repo` column
(ALTER-migrated on open; pre-coupling runs read ''), shown in the UI. With the trace at
home, `_trace_exemptions()` against the target is empty by construction — the target's
`git clean` cannot touch the trace. 101 tests passing (8 new: migration, repo column,
cwd/secrets split, env_dir default, env_dir forwarding, exemption emptiness, `prompt
--repo`, legacy-UI render). Live smoke:
`plan-build-test --repo /tmp/cp-demo` against a repo holding only `.git` + `README.md` —
the plan commit landed there, run `b47b6d3c` was recorded at home with
`repo: /tmp/cp-demo`, the checkpoint refusal stayed intact, and nothing (roster, secrets,
trace) leaked into the target.

Milestone: the pre-PR dogfood `build-review` contributed code to this change, not just
verdicts. Its fixer caught two real gaps in the working tree — `cmd_prompt` not honoring
`--repo`, and the query-only UI crashing with `IndexError` on pre-`repo` legacy traces —
and landed both fixes with their own tests (the round-2 gate then blocked only on this
note's stale test count, which the human corrected). "The factory reviews and fixes
itself" now includes fixing the human's work.

The live smoke then surfaced a real factory bug [historical: written while the trace still
lived inside the target repo — after the decoupling note below, the trace stays at home and
this scenario cannot recur]: in a target repo without a
`.gitignore`, the writes boundary's rollback (`git clean`) treated `data/factory.db`
as agent debris and deleted the trace mid-run — the open connection kept writing to an
unlinked inode and the trace was lost silently (it had survived until now only because
`data/` is gitignored in this repo). Fix: `_trace_exemptions()` exempts the trace db
plus its WAL sidecars from every gate rollback, from both commits (`git reset -q` after
`add -A`), and from the reviewer diff collector. Regression test: the whole chain runs
in an ignore-free repo and a fresh connection reads the full trace while no commit
carries `data/`. Verified live: the no-TTY smoke leaves run `e0e4a6f4` with all 7 phases
recorded (844 pi events) and the plan commit carries only `specs/<run>-plan.md`.

### Closure note — the board: Phases 6, 7 and 8 in one hull (added 2026-11-08)

Phases 6–8 shipped together in one PR (the owner's call: the board is one product,
not three increments). `factory serve` is now the long-lived daemon the PLAN
described: kanban at loopback with columns **Backlog / Working / In PR** plus a
separate Archived section, a create-task form fed by the roster's `repos:` section,
one worker per repo (FIFO within a repo, repos concurrent, `max_concurrent_runs`
as the global RAM cap), and the Phase-4 run detail behind every card.

- **Phase 6 — the queue.** `tasks` table (cards with `origin owner|system`,
  `runnable`, `branch`, `pr_url`, provenance `origin_run`). The dispatcher claims
  one runnable card per repo, FIFO by `created_at, rowid` — rowid is the true
  insertion order; same-second `created_at` ties would otherwise shuffle the queue.
  System cards land unrunnable: the factory proposes, the owner promotes.
- **Phase 7 — pauses as rows.** `pauses` table (`questions | plan_approval |
  pr_approval`, request/answer/status). The chain's human-gate callbacks block on
  an event bound to the pause row; the owner answers from the browser and the SAME
  pi session resumes (`{run}-planner` on both sides of a correction, verified live).
  The planner's envelope gained `questions`; asking with nobody wired fails the
  plan gates honestly. The stdin checkpoint stays CLI-only.
- **Phase 8 — delivery + discovered work.** Branch per task cut from
  `origin/<default>` after a fetch (never from HEAD — the previous task's branch —
  nor from a stale local default; `checkout -B` resets a rerun's stale branch even
  when it is the checked-out one). On the PR-approval pause: commit, push,
  `gh pr create`, card to In PR; merged PRs auto-archive (poller). Envelopes gained
  `discovered` — out-of-scope findings become Backlog cards with `origin_run`
  provenance, deduped per repo+title. Agents never run git; neither does anything
  outside these deterministic code paths.
- **Crash honesty.** Startup reaper settles runs a dead daemon left `running`,
  fails their open phases, expires their waiting pauses, flags their cards for a
  rerun. Shutdown expires open pauses so workers unwind and runs settle failed
  instead of hanging forever. Deployment: `deploy/factory.service.sample` (systemd
  user service, loopback + SSH tunnel/Tailscale).

Live verification: two real runs through the board — a demo repo task
(plan approval → build/test → PR approval → push OK → `gh pr create` fails
honestly against a non-GitHub origin: the card shows the error and a rerun button),
and a task against a clean clone of this very repo: plan approved from the board,
pytest green, PR approved, **PR opened on GitHub** (ricardomm85/software-factory#7)
and the card settled in In PR. A live plan correction re-entered the same planner
session and the corrected plan incorporated the owner's constraint. The questions
pause is exercised by tests (round trip, same-session resume, bounded loop,
CLI-without-questions); live, both planners judged the requests answerable and
planned straight away. 147 tests passing (44 new).

Dogfood: the pre-PR gate ran the full review→score→gate→fix loop FOUR times to a
verdict (six runs total; two flaked on lens timeouts against this ~2k-line diff —
an operational finding for Phase 9, recorded at the end). First verdict: 4 findings ≥50 —
duplicated `BuildOutput` definitions (silently shadowed), planner prompt text
hardcoded in Python against the prompts-as-data rule, a rerun stalling on its own
branch, and branches cut from an un-fetched local default. The fixer resolved the
first three in-session (prompts became `planner_answers.md`/`plan_correction.md`
templates via `_correction`, exactly the repo convention); the stale-base finding
survived re-review at 90 and was fixed by hand (fetch + `origin/<default>` +
`checkout -B`). Second verdict: 3 more ≥50 — a misleading failure message when the
question rounds exhaust, failed/declined runs leaving the target repo dirty so the
rerun button hit a clean-tree wall forever, and `discovered` findings dropped when
the chain raised instead of returning. The fixer took all three: the board now
restores the worktree it dirtied (deterministic git, exempt paths preserved — a
decline discards the attempt, whose trace stays in the run), collects discovered
cards in a `finally`, and tells the two question cases apart. Re-review kept one
finding (90): a rerun after a partial delivery (push OK, `gh` failed) could never
push again — non-fast-forward against the previous attempt's branch. Fixed by hand:
`push --force-with-lease` on the board-owned ref. Third verdict: 3 more ≥50 — cards
failing preflight stuck in Working with no action button, the startup reaper owning
runs it did not launch (now stamped `sessions.launched_by` and filtered), and real
CSRF: any website the owner browses could POST to the loopback board and resolve a
pause the owner never saw (refused by an Origin/Host check on every mutating
endpoint; curl and scripts pass). Re-review kept one finding (85): daemon shutdown
neither stopped nor settled in-flight agent subprocesses — fixed by hand with a
`kill_live_agents()` registry in `agent_pi` wired into `Board.stop()`. Fourth
verdict: 1 finding ≥50, scored 95 and REPRODUCED live — the one shared sqlite
connection ran across the dispatcher, per-repo workers, each worker's pi event
pump thread, the poller and the HTTP handlers; CPython's sqlite3 is not safe for
that (statement cache races, interleaved implicit transactions), and the board's
own suite flaked on it. The fixer closed it in-cycle with `LockedConnection` —
every statement executes and materializes under one lock, the cursor frozen before
release — plus the ownership migration. Final verdict: **accepted, no findings
scored >=50**. 157 tests passing (54 new). Operational note for Phase 9: the
review-bugs lens needs generous timeouts on diffs this size — two of the six runs
died on it — so Phase 9 should design a bounded review surface or a lens timeout
budget before scaling the board.

### Phase 9 design note — the board gets a front door (added 2026-11-08)

The owner's mental model, verbatim in spirit: *"al entrar a software factory yo
vería un listado de los repos a los que tengo acceso; entro a uno y veo las tareas
pendientes; la primera vez, un onboarding donde pongo el token de GLM y el de
GitHub."* What changes:

- **`/setup` runs once, when it matters.** The board detects a fresh install (no
  `.env`, or no repos registered) and routes to setup instead of an empty board.
  Three inputs: Z.AI token (validated with one real, cheap API call before
  anything is written), GitHub token (validated against the API; it drives push
  and `gh pr create` via `GH_TOKEN` for the daemon's subprocesses — no
  interactive `gh auth login` on a headless box), and the clone base folder
  (default: `~/Projects`). Only after both validations pass does setup write
  `.env` (chmod 600) and the roster's `repos:` section.
- **Config-as-data survives.** The UI *writes* `config.yaml` and `.env`; the files
  stay the source of truth — hand edits still win, and the board reads them
  fresh. Secrets are masked in every response and never logged.
- **Repos come from GitHub.** The add-repo flow lists the owner's repositories
  (`gh repo list`), and for each chosen one: detect an existing local clone
  (search the base folder by name) or clone to a chosen path, then register it.
  The factory still works on local clones — GitHub is the catalog, the disk is
  the workspace.
- **Repo-first navigation.** Home = one card per managed repo (name, per-column
  card counts, last PR). A repo page is its kanban (the existing columns,
  scoped) plus its create-task form. Run detail, pause pages and the runs index
  stay as they are.
- **What this is NOT.** No multi-tenancy, no accounts: one owner, localhost.
  Onboarding is convenience, not a security boundary.

### Phase 9 design note — the board's face (added 2026-11-08, shipped first)

The front door needs a legible room behind it: the owner's verdict on the first real
uses was *"no me entero de nada"* — the board worked but did not read. Decisions
taken with the owner, now the visual contract:

- **Structure.** Rendering moved out of `board.py`/`ui.py` into a `factory/web/`
  package (`scaffold` = one page skeleton shared by every page; `kanban` = board,
  cards, pauses; `runs` = runs index + run timeline) with real assets in
  `static/` (`style.css`, `app.js`). No frameworks, still stdlib-only; the
  `factory/ui.py` module is gone.
- **Dark only.** One theme, CSS variables; semantic colors stay: green = passed/
  accepted, red = failed, orange = running, blue = waiting on you.
- **Live without reloads.** No `meta refresh` on JS pages: a `[data-live]` region
  polls its fragment endpoint (`/fragment/board`, `/fragment/run/{id}`,
  `/fragment/runs`) every 4s and swaps only on change, keeping open `<details>`
  open. The page and its fragment render from the same `Board.board_snapshot()`,
  so they cannot disagree. `<noscript>` keeps a meta-refresh fallback.
- **Cards say what is happening.** A Working card names its current phase in
  plain English ("building · 03:24", ticking client-side from a `data-started-epoch`),
  pauses keep the amber strip ("waiting on you"), and In PR cards state the
  poller out loud: "auto-archives on merge — checks every 60s".
- **The run page is a timeline.** One step per phase (icon: done/failed/running/
  waiting), human labels via a phase-name map (`build:r1` → building,
  `review:scorer:r2` → reviewing (scorer)), per-step details collapsed (that
  phase's events, gates, error); the current step is open and pulsing.
- **Archived is a searchable table**, not a collapsed list (cap raised 50 → 200).
- **Forms and filters survive refreshes**: the new-task form and the archived
  table sit outside the polled region.

## References

- Video: https://www.youtube.com/watch?v=haUfb1ievTE (IndyDevDan)
- sssf: https://github.com/disler/super-simple-software-factory (the `example` branch has the factory stamped into a demo repo)
- Warp Factories: https://try.warp.dev/software-factories/software-factory-v2 · Factory.com: https://factory.com
- Huntley: https://ghuntley.com/rad · MetaGPT: https://github.com/foundationagents/metagpt
- Z.AI × Claude Code: https://docs.z.ai/devpack/tool/claude · OpenCode CLI: https://opencode.ai/docs/cli
