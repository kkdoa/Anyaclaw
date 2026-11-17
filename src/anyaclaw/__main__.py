"""Allow ``python -m anyaclaw``."""

from __future__ import annotations

from anyaclaw.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
