from __future__ import annotations

import hashlib
import re


IDENTITY_COOKIE_NAME = "portfolio_identity"
IDENTITY_COOKIE_MAX_AGE_SECONDS = 10 * 365 * 24 * 60 * 60
DEFAULT_LEADERBOARD_KEY = "xp"
_KEY_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def leaderboard_key_id(key: str) -> str:
    """Return a cookie-safe, fixed-size identity for an arbitrary non-empty key."""
    if not isinstance(key, str) or key == "":
        raise ValueError("key 不能为空，且必须是字符串")
    try:
        encoded = key.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("key 包含无效字符") from exc
    return hashlib.sha256(encoded).hexdigest()


def valid_leaderboard_key_id(value: str | None) -> bool:
    return bool(value and _KEY_ID_PATTERN.fullmatch(value))


DEFAULT_LEADERBOARD_KEY_ID = leaderboard_key_id(DEFAULT_LEADERBOARD_KEY)
