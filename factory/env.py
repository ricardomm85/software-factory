"""Minimal .env loader for the factory's secret file.

The single allowed secret location is ``~/.config/factory/.env`` (chmod 600,
outside any repository so it can never be committed). The factory loads it and
injects the values into every agent subprocess environment.
"""

from pathlib import Path

DEFAULT_ENV_PATH = Path.home() / ".config" / "factory" / ".env"


def load_env(path: Path = DEFAULT_ENV_PATH) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines into a dict. Blank lines and ``#`` comments are ignored."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values
