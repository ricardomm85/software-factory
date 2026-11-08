"""Load and validate the factory roster config (``config.yaml``).

The roster answers one question per entry: who is this agent. One agent, one
purpose. Model and effort are properties of the role; call sites name agents,
never models.
"""

import re
from pathlib import Path

import yaml

# Models are always written provider/model-id — a bare id can match several providers.
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")

# Thinking levels accepted by the pi CLI (also usable as a ``:level`` model suffix).
THINKING_LEVELS = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}


class ConfigError(ValueError):
    """Raised when the roster config is missing required fields or has bad values."""


def load_config(path: Path) -> dict:
    """Read and validate a roster config file. Raises ConfigError on any problem."""
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigError(f"config root must be a mapping, got {type(data).__name__}")
    validate_config(data)
    return data


def validate_config(cfg: dict) -> None:
    """Validate roster invariants. Raises ConfigError with a precise message."""
    defaults = cfg.get("defaults")
    if not isinstance(defaults, dict):
        raise ConfigError("missing required section: defaults")

    coding_agent = defaults.get("coding_agent")
    if coding_agent != "pi":
        # v1 runs pi only; anything else fails before anything spawns.
        raise ConfigError(f"defaults.coding_agent must be 'pi', got {coding_agent!r}")

    _validate_model(defaults.get("model"), "defaults.model")
    _validate_thinking(defaults.get("thinking"), "defaults.thinking")

    data_dir = defaults.get("data_dir", "data")
    if not isinstance(data_dir, str) or not data_dir.strip():
        raise ConfigError(f"defaults.data_dir must be a non-empty string (got {data_dir!r})")

    test_command = defaults.get("test_command")
    if test_command is not None and (not isinstance(test_command, str) or not test_command.strip()):
        raise ConfigError("defaults.test_command must be a non-empty string (the real one, not a placeholder)")

    cap = defaults.get("max_concurrent_runs", 1)
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
        raise ConfigError(
            f"defaults.max_concurrent_runs must be an integer >= 1 (got {cap!r}); "
            "it caps simultaneous runs globally — size it to the box's RAM"
        )

    agent_timeout = defaults.get("agent_timeout_s", 300)
    if not isinstance(agent_timeout, int) or isinstance(agent_timeout, bool) or agent_timeout < 1:
        raise ConfigError(
            f"defaults.agent_timeout_s must be an integer >= 1 (got {agent_timeout!r}); "
            "it is each agent call's budget in seconds — planners thinking hard need more than 300"
        )

    review = defaults.get("review", True)
    if not isinstance(review, bool):
        raise ConfigError(
            f"defaults.review must be a boolean — unquoted on/off or true/false (got {review!r}); "
            "do not quote it: YAML reads bare on/off as true/false"
        )

    lenses = defaults.get("review_lenses", ("review-conventions", "review-bugs"))
    if (
        not isinstance(lenses, (list, tuple))
        or not lenses
        or not all(isinstance(n, str) and n.strip() for n in lenses)
    ):
        raise ConfigError(
            f"defaults.review_lenses must be a non-empty list of roster agent names (got {lenses!r}); "
            "one reviewer per lens, e.g. [review-conventions, review-bugs]"
        )

    repos = cfg.get("repos", [])
    if not isinstance(repos, list):
        raise ConfigError("repos must be a list of mappings (name, path, optional test_command)")
    seen_repos: set[str] = set()
    for i, entry in enumerate(repos):
        if not isinstance(entry, dict):
            raise ConfigError(f"repos[{i}] must be a mapping with 'name' and 'path'")
        name = entry.get("name")
        if not name or not isinstance(name, str):
            raise ConfigError(f"repos[{i}] must have a non-empty string 'name'")
        if name in seen_repos:
            raise ConfigError(f"duplicate repo name: {name!r}")
        seen_repos.add(name)
        repo_path = entry.get("path")
        if not repo_path or not isinstance(repo_path, str):
            raise ConfigError(f"repos.{name} must have a non-empty string 'path'")
        repo_test = entry.get("test_command")
        if repo_test is not None and (not isinstance(repo_test, str) or not repo_test.strip()):
            raise ConfigError(f"repos.{name}.test_command must be a non-empty string when given")

    agents = cfg.get("agents")
    if not isinstance(agents, list) or not agents:
        raise ConfigError("missing required section: agents (non-empty list)")

    seen: set[str] = set()
    for i, agent in enumerate(agents):
        if not isinstance(agent, dict):
            raise ConfigError(f"agents[{i}] must be a mapping")
        name = agent.get("name")
        if not name or not isinstance(name, str):
            raise ConfigError(f"agents[{i}] must have a non-empty string 'name'")
        if name in seen:
            raise ConfigError(f"duplicate agent name: {name!r}")
        seen.add(name)
        _validate_model(agent.get("model"), f"agents.{name}.model")
        _validate_thinking(agent.get("thinking"), f"agents.{name}.thinking")


def _validate_model(model, where: str) -> None:
    if model is None:
        return  # inherits defaults
    if not isinstance(model, str) or not MODEL_ID_RE.match(model):
        raise ConfigError(
            f"{where} must be written 'provider/model-id' (got {model!r}); "
            "a bare id can match several providers"
        )


def _validate_thinking(thinking, where: str) -> None:
    if thinking is None:
        return
    if isinstance(thinking, bool):
        # YAML 1.1 parses bare on/off/yes/no as booleans — require quoting.
        raise ConfigError(
            f"{where}: bare off/on/yes/no are parsed as booleans by YAML; "
            f"quote the value, e.g. {where}: 'off'"
        )
    if thinking not in THINKING_LEVELS:
        allowed = "|".join(sorted(THINKING_LEVELS))
        raise ConfigError(f"{where} must be one of: {allowed} (got {thinking!r})")


def resolve_agent(cfg: dict, name: str | None = None) -> dict:
    """Return the effective settings for an agent, applying defaults.

    ``name=None`` resolves the defaults themselves — how Phase 0's spawn script
    picks its model. Unknown agent names fail here, not halfway into a chain.
    """
    defaults = cfg["defaults"]
    if name is None:
        return {"name": "default", "model": defaults["model"], "thinking": defaults.get("thinking")}
    for agent in cfg.get("agents", []):
        if agent["name"] == name:
            return {
                "name": name,
                "model": agent.get("model", defaults["model"]),
                "thinking": agent.get("thinking", defaults.get("thinking")),
                "purpose": agent.get("purpose", ""),
            }
    known = ", ".join(a["name"] for a in cfg.get("agents", []))
    raise ConfigError(f"unknown agent {name!r} (known agents: {known})")


def resolve_repos(cfg: dict) -> list[dict]:
    """The board's repo list: name, resolved path, effective test command.

    ``test_command`` falls back to ``defaults.test_command`` — the roster default
    is the real suite, a per-repo entry overrides it. Paths are expanded and
    resolved so cards, the trace and the repo always agree on one spelling.
    """
    defaults = cfg["defaults"]
    out = []
    for entry in cfg.get("repos", []):
        test_command = entry.get("test_command") or defaults.get("test_command") or ""
        out.append({
            "name": entry["name"],
            "path": Path(entry["path"]).expanduser().resolve(),
            "test_command": test_command,
        })
    return out
