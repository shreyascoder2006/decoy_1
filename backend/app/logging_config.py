"""Logging setup with a guard against leaking secrets into log output.

The filter is a backstop, not a licence to log sensitive values: call sites are
written never to pass audio bytes, headers or credentials to the logger in the
first place.
"""

from __future__ import annotations

import logging
import re

from app.config import get_settings

REDACTED = "[REDACTED]"

_PATTERNS = [
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)(authorization\s*[:=]\s*)\S+"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)\S+"),
]


class SecretRedactingFilter(logging.Filter):
    """Replace credential-shaped substrings in formatted log records."""

    def __init__(self, extra_literals: tuple[str, ...] = ()) -> None:
        super().__init__()
        self._literals = tuple(v for v in extra_literals if v and len(v) >= 8)

    def _scrub(self, text: str) -> str:
        for literal in self._literals:
            text = text.replace(literal, REDACTED)
        for pattern in _PATTERNS:
            text = pattern.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never break logging
            return True
        scrubbed = self._scrub(message)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = ()
        return True


def configure_logging(level: int = logging.INFO) -> None:
    settings = get_settings()
    secret_filter = SecretRedactingFilter((settings.resemble_api_key,))

    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
        root.addHandler(handler)
    for handler in root.handlers:
        handler.addFilter(secret_filter)
    root.setLevel(level)
