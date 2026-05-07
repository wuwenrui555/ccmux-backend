"""ccmux — Claude-tmux backend library.

**Import from `ccmux.api` for the public surface.** The package root is
deliberately bare of re-exports — `from ccmux import X` fails loudly
rather than silently routing through a back door.

The only side effect at import time is pointing ``claude_code_state`` at
the same configuration directory as ccmux: when ``$CCMUX_DIR`` is set,
``$CLAUDE_CODE_STATE_DIR`` inherits it; otherwise both default to
``~/.ccmux``. ``setdefault`` is used so a caller (e.g. a test) can pin
``$CLAUDE_CODE_STATE_DIR`` explicitly.
"""

from __future__ import annotations

import os

from .util import ccmux_dir

os.environ.setdefault("CLAUDE_CODE_STATE_DIR", str(ccmux_dir()))
