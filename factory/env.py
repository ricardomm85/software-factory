"""Secret loading for the factory.

Secrets live in a repo-local ``.env`` (gitignored; the committed ``.env.sample``
documents the keys). There is no global fallback on purpose: one repo, one key
set — explicit over implicit.
"""

from pathlib import Path

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


def load_env(repo_dir: Path) -> dict[str, str]:
    """Parse the repo-local ``.env``. A missing file yields an empty dict."""
    return parse_env(repo_dir / REPO_ENV_NAME)
