# Plan b65f571a — raise `defaults.agent_timeout_s` from 600 to 3600

## Context

Real run evidence from today's board runs: with `agent_timeout_s: 600`, the planner
needed 540s and passed, but the builder timed out at exactly 600012 ms on a
multi-file refactor. The owner has set the floor at one hour (3600s).

The knob is validated in `factory/config.py:64-68` (`integer >= 1`, no upper bound),
so no code change is needed. Scope is **config-only**.

## Files to touch

Exactly one file: `config.yaml`. Nothing else — no Python, no tests, no docs.

## Change

In the `defaults:` block of `config.yaml`, replace lines 12–13:

```yaml
  agent_timeout_s: 600             # seconds each agent call may run — planners thinking high
                                   # need more than 300 (three real board runs died at 300s)
```

with:

```yaml
  agent_timeout_s: 3600            # seconds each agent call may run — one hour: planner
                                   # passed at 540s, builder died at 600012 ms on a
                                   # multi-file refactor (real run, today)
```

Notes on style (match the neighboring comments in the same block):
- Keep the `#` comment column aligned with the neighboring keys (`checkpoint:`,
  `max_concurrent_runs:` — i.e. `#` starts at the same column as before; since
  `3600` is one character longer than `600`, drop one space after the value).
- Comment is English, lowercase, em-dash style, and cites the run evidence that
  justifies the number, like the old comment did.
- Do not reorder, re-indent, or reformat any other line of the file.

## Acceptance criteria

1. `grep -n "agent_timeout_s" config.yaml` returns exactly one line and it reads
   `agent_timeout_s: 3600` with the new comment.
2. `git diff --stat` shows only `config.yaml` modified, and `git diff config.yaml`
   touches only that key's value and its comment lines (no other hunks).
3. The config still loads and validates:
   ```
   uv run python -c "from pathlib import Path; from factory.config import load_config; \
   print(load_config(Path('config.yaml'))['defaults']['agent_timeout_s'])"
   ```
   prints `3600` with no `ConfigError`.
4. The test suite is unaffected (config fixture-based, but run it to be safe):
   `uv run pytest -q tests/test_config_repos.py` passes.
