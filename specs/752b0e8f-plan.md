# Plan — README quickstart: one sentence about the task board

**Scope:** docs-only. Exactly one existing file modified (`README.md`); no new files,
no code, no config, no tests touched.

## Context

- The request: add **one short sentence** about the task board (the `factory serve`
  command: create tasks in a local kanban, answer agent questions from the browser,
  approve the PR push) to the **Quickstart section** of `README.md`, plus **one example
  command line**. Nothing else may change.
- The task board is the 2026-11-07 pivot described in `PLAN.md` (web-board branch,
  Phase 6–8 design note): `factory serve` is the daemon that owns execution and the
  kanban (Backlog / Working / In PR + Archived), parks cards to ask (questions,
  plan approval, PR approval), and delivers via PR push on owner approval.
- The `factory serve` command does **not** exist in code yet (today only `factory ui`
  is implemented). This README change is intentionally docs-ahead-of-code, requested
  explicitly by the owner. **Do not** add "coming soon" caveats, **do not** implement
  the command, **do not** annotate anywhere else in the README.
- `factory serve` grows out of `factory ui` per the plan (same stdlib server,
  loopback-only, same default port 8666 — already shown in the README's Demo section),
  so referencing `http://127.0.0.1:8666` in the example line is consistent.
- Language policy (AGENTS.md): English only.

## Change

In **`README.md`**, section `## 🚀 Quickstart`, locate this closing paragraph:

```
That's it — [pi](https://github.com/badlogic/pi-mono) is the coding agent (installed
separately), GLM-5.3-flash is the brain, your repo is the workplace.
```

Immediately after it (before the `## 🧰 The commands` heading), insert EXACTLY this
block — one sentence, one fenced example command line, one blank line between each
element, matching the file's existing console-block style:

```

Prefer the browser: the `factory serve` task board is where you create tasks as
cards in a local kanban, answer the factory's questions, and approve the PR push.

```console
$ factory serve          →  the board at http://127.0.0.1:8666
```
```

(That is: a blank line, the paragraph above, a blank line, a ` ```console ` fenced
block containing the single line `$ factory serve          →  the board at http://127.0.0.1:8666 `,
a blank line before the next heading — preserving the file's spacing conventions.)

Notes on the wording, already decided (do not re-litigate):
- It is one sentence and covers exactly the three capabilities from the request:
  kanban task creation, browser Q&A, PR push approval.
- The example line reuses the README's established `$ <cmd> → <effect>` console style
  (same as the `factory ui` demo line) and the existing documented loopback port.

## Files

| Path | Action |
|---|---|
| `README.md` | modify — insert one paragraph + one console block in Quickstart only |

## Acceptance criteria

1. `git status --porcelain` shows **only** ` M README.md`; no other file added,
   deleted, or modified (ignore pre-existing untracked `specs/` files if present —
   do not stage or revert them).
2. `git diff README.md` contains exactly one added hunk, located between the
   "That's it — …" paragraph and the `## 🧰 The commands` heading; zero deletions.
3. The added text matches the Change section verbatim (modulo the fence rendering):
   exactly one new sentence mentioning `factory serve`, kanban task creation, browser
   Q&A, and PR push approval — plus exactly one example command line
   (`$ factory serve          →  the board at http://127.0.0.1:8666`).
4. No other README section is altered: the Demo, The commands table, Ground rules,
   Roadmap, Honest limitations, Docs, Credits sections are byte-identical
   (`git diff README.md` shows no `-` lines).
5. The sentence count of the addition is 1 (the console line is a command, not prose).
6. Markdown integrity: fenced code blocks remain balanced (```bash, ```mermaid,
   ```console counts unchanged except +1 console block).
7. `uv run pytest -q` still passes (docs-only change; cheap regression check).

## Explicitly out of scope

- Adding a `factory serve` row to the `## 🧰 The commands` table (the request limits
  the addition to Quickstart).
- Touching `PLAN.md`, `factory/`, `tests/`, `config.yaml`, or any other file.
- Implementing the `factory serve` command or any board code.
- Caveats, "coming soon" markers, or status hedging in the README.
