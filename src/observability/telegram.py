"""
Telegram alerting — send P0 alerts to Telegram via webhook.

Uses a Telegram bot configured via environment variables:
  - TELEGRAM_BOT_TOKEN: bot token from @BotFather
  - TELEGRAM_CHAT_ID: target chat/group ID

If either variable is unset, alerts are silently dropped.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Dict, Optional

# ── Configuration (from environment) ────────────────────────────────────────
_BOT_TOKEN: Optional[str] = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID: Optional[str] = os.environ.get("TELEGRAM_CHAT_ID", "")
_BASE_URL: str = "https://api.telegram.org/bot"
_ENABLED: bool = bool(_BOT_TOKEN and _CHAT_ID)

# Cache last send time for rate limiting
_last_send: float = 0.0


def configure_telegram(bot_token: str, chat_id: str) -> None:
    """Configure Telegram credentials at runtime (overrides env vars)."""
    global _BOT_TOKEN, _CHAT_ID, _BASE_URL, _ENABLED, _last_send
    _BOT_TOKEN = bot_token
    _CHAT_ID = chat_id
    _BASE_URL = f"https://api.telegram.org/bot{bot_token}"
    _ENABLED = bool(bot_token and chat_id)
    _last_send = 0.0


def telegram_alert(
    title: str,
    data: Optional[Dict[str, Any]] = None,
    parse_mode: str = "Markdown",
) -> bool:
    """Send a P0 alert via Telegram webhook.

    Args:
        title: Alert title/message (supports Markdown).
        data: Optional structured context to append as a code block.
        parse_mode: Telegram parse mode (Markdown or HTML).

    Returns:
        True if sent, False if skipped (not configured or rate-limited).
    """
    global _last_send

    if not _ENABLED:
        return False

    # Rate-limit: at most 1 message per 10 seconds
    import time
    now = time.time()
    if now - _last_send < 10.0:
        return False
    _last_send = now

    text = title
    if data:
        text += "\n```\n" + json.dumps(data, indent=2, default=str) + "\n```"

    payload = {
        "chat_id": _CHAT_ID,
        "text": text[:4096],  # Telegram 4096 char limit
        "parse_mode": parse_mode,
        "disable_notification": False,
    }

    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{_BASE_URL}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            return result.get("ok", False)
    except Exception:
        return False