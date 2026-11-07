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
