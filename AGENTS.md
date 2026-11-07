# AGENTS.md

Working conventions for any AI agent (or human) contributing to this repository.

## Language policy

**All documentation and code in this repository MUST be written in English**, including:

- Documentation: `*.md` files, docstrings, code comments
- Code: identifiers, log messages, error messages
- Commit messages, PR titles and descriptions
- Prompts and agent-facing config shipped in the repo (`config.yaml`, prompt templates, envelope descriptions, gate notes)

Rationale: this repo is written, read, and maintained largely by coding agents. English keeps
generation and comprehension at their best ("stay in distribution") and keeps the project shareable.

The conversation with the project owner happens in Spanish — that is chat only.
Never let it leak into repository artifacts.

## Project in one paragraph

A personal software factory: a Python orchestrator that owns the agentic SDLC loop
(plan → build → test → review) over local repos, with a coding agent CLI as bounded nodes
inside it. Inspired by the *shape* of [disler/sssf](https://github.com/disler/super-simple-software-factory):
phases as context managers, typed pydantic envelopes, post-hoc gates, SQLite (WAL) trace.
Read [`PLAN.md`](PLAN.md) before proposing changes — phase sequencing and scope decisions live there.

## Ground rules

- Python 3.12+, managed with `uv`; SQLite (WAL) for state. No heavyweight frameworks.
- Config and prompts are data (YAML/Markdown), never hardcoded in Python.
- Every phase defaults to `fail`; success must be earned (clean exit + green gates).
- On bad agent output, correct within the same session (resume by id). Never cold-restart.
- The deterministic code owns sequencing, retries, and acceptance; agents own the work
  inside one bounded phase. "Agent proposes, code disposes."
