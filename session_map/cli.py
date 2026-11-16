"""The `session-map` command (also `python3 -m session_map`).

- `session-map snapshot [--problems]` prints the snapshot as JSON (or only the texts of the sources that failed);
- `session-map prototype [--out PATH]` writes the page with the snapshot of now embedded (default ./prototype.html);
- `session-map serve` runs the local server in the foreground;
- `session-map open` reuses the live server or starts one, then opens the map.

Exit codes: 0 ok, 1 error, 2 bad arguments. Never a traceback.
"""

from __future__ import annotations

import argparse
import sys

from . import server, snapshot


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="session-map",
                                 description="A live map of the agent sessions here (Claude Code and Codex).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot", help="print the snapshot as JSON")
    s.add_argument("--problems", action="store_true", help="print only the texts of the sources that failed")
    p = sub.add_parser("prototype", help="write the page with the snapshot of now embedded")
    p.add_argument("--out", default="prototype.html", help="where to write it (default ./prototype.html)")
    sub.add_parser("serve", help="run the local server (127.0.0.1, SESSION_MAP_PORT, default 9131)")
    sub.add_parser("open", help="reuse the live server or start one, and open the map")
    return ap


def main(argv=None) -> int:
    try:
        a = _parser().parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    if a.cmd == "snapshot":
        return snapshot.main(["snapshot"] + (["--problems"] if a.problems else []))
    if a.cmd == "prototype":
        return snapshot.main(["prototype", "--out", a.out])
    try:
        return server.serve() if a.cmd == "serve" else server.open_map()
    except KeyboardInterrupt:
        return 0
    except ValueError as exc:  # a bad SESSION_MAP_PORT or SESSION_MAP_IDLE_S: the message names it
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
