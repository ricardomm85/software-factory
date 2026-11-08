"""The factory's visual surface: rendering, split from logic.

Everything the browser sees lives here — one page scaffold, real static
assets (``static/style.css``, ``static/app.js``), and render functions that
take data in (a ``Board.board_snapshot()`` dict or a read-only trace
connection) and return HTML. ``factory.board`` keeps the state, the
execution logic and the HTTP plumbing; nothing in this package mutates.
"""

from . import kanban, runs, scaffold  # noqa: F401

__all__ = ["kanban", "runs", "scaffold"]
