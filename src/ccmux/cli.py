"""Entry point for the `ccmux hook` CLI.

Installed via `pyproject.toml [project.scripts] ccmux = "ccmux.cli:main"`.
The only subcommand is `hook` — the Claude Code SessionStart hook that
writes `~/.ccmux/window_bindings.json`. See `ccmux.hook` for details.
"""

import sys


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "hook":
        from .hook import hook_main

        hook_main()
        return

    print("usage: ccmux hook [--install]", file=sys.stderr)
    sys.exit(2)
