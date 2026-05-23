"""Current public RedSky `key` param.

Spec §3 / R5: Target rotates this 40-hex key periodically; when it rotates,
the source returns non-200 or a shape mismatch and ``scripts/refresh-redsky-key.sh``
overwrites this file in place.

Initial value sourced from public open-source Target.com scraper projects
that decode it from Target's homepage JS bundle (commonly seen across 2024–2025
GitHub repos that hit ``redsky.target.com``). The env var override exists so
that a stale committed key never blocks a live run — refresh the env first,
then ``refresh-redsky-key.sh`` to update the file.
"""

from __future__ import annotations

import os


_DEFAULT_REDSKY_KEY = "9f36aeafbe60771e321a7cc95a78140772ab3e96"

REDSKY_KEY: str = os.environ.get("TARGET_REDSKY_KEY", _DEFAULT_REDSKY_KEY)
