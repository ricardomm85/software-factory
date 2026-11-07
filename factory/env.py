"""Secret loading for the factory.

Two-layer convention, like git config:

1. Repo-local ``.env`` (gitignored; the committed ``.env.sample`` documents the keys)
2. Global fallback ``~/.config/factory/.env`` (chmod 600, outside any repository)

Repo-local values win. The global layer exists so every repo where the factory
runs shares one key until that repo declares its own.
"""

from pathlib import Path

GLOBAL_ENV_PATH = Path.home() / ".config" / "factory" / ".env"
REPO_ENV_NAME = ".env"


def parse_env(path: Path) -> dict[str, str]:
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


def load_env(repo_dir: Path | None = None) -> dict[str, str]:
    """Merge the global fallback under the repo-local ``.env`` (repo wins)."""
    merged = parse_env(GLOBAL_ENV_PATH)
    if repo_dir is not None:
        merged.update(parse_env(repo_dir / REPO_ENV_NAME))
    return merged
