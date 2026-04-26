"""Append-only audit log of admin-driven actions.

Why a dedicated module
----------------------

Until this PR, admin commands changed config or subscriptions with no
durable trace beyond the bot log file. That's enough to debug crashes
but useless for "who turned anonymous posting off yesterday?"  The
:class:`AuditLog` here gives us a single chokepoint that:

* persists every recorded action to the ``audit_log`` SQLite table
  (see :mod:`storage.db`), and
* optionally posts a one-line summary to a Zulip stream/topic so other
  admins see actions in real time.

Scope
-----

The audit log is for **explicit admin commands** -- ``!anon set …``,
``!access add/remove``, ``!subscribe …`` etc. It is intentionally
**not** wired into anonymous-posting submissions: doing so would
create a lookup table from "anon message" → real user id, which
defeats the privacy contract those features promise. If we ever want
visibility there, it should be a separate, non-de-anonymising signal.

Failures
--------

Persistence is best-effort: if the SQLite write fails, we log and let
the admin command continue (refusing to apply the user's change just
because we couldn't audit it would be a worse outcome than a missing
log line). The broadcast is similarly best-effort: a Zulip API hiccup
must never roll back a config update that already took effect.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from config import ConfigManager
from core.client import ClientProtocol
from storage.db import Storage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuditEntry:
    """One row of the audit log, hydrated for callers."""

    id: int
    ts: datetime
    action: str
    actor_id: int | None
    target: str | None
    details: dict[str, Any] | None


@dataclass
class AuditLog:
    """Records admin actions to durable storage and (optionally) a stream."""

    storage: Storage
    config_mgr: ConfigManager
    client: ClientProtocol

    def _audit_cfg(self) -> dict[str, Any]:
        return self.config_mgr.get().get("audit", {}) or {}

    async def record(
        self,
        action: str,
        *,
        actor_id: int | None = None,
        target: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> int:
        """Persist an audit entry and (optionally) broadcast it.

        Returns the new row id, or ``-1`` if auditing is disabled in
        config. Callers should treat the return value as opaque.
        """
        if self._audit_cfg().get("enabled", True) is False:
            return -1

        details_json = json.dumps(details, sort_keys=True, default=str) if details else None
        try:
            entry_id = await self.storage.insert_audit(
                action=action,
                actor_id=actor_id,
                target=target,
                details_json=details_json,
                ts=datetime.now(UTC),
            )
        except Exception:
            # Don't let audit persistence break the actual admin command;
            # log loudly and move on so the operator still sees a signal.
            logger.exception("audit persistence failed for action=%s actor=%s", action, actor_id)
            return -1

        await self._maybe_broadcast(entry_id, action, actor_id, target, details)
        return entry_id

    async def recent(self, limit: int = 50) -> list[AuditEntry]:
        """Return the newest ``limit`` audit entries (newest first)."""
        rows = await self.storage.recent_audit(limit=limit)
        out: list[AuditEntry] = []
        for r in rows:
            details_raw = r.get("details")
            details: dict[str, Any] | None = None
            if isinstance(details_raw, str) and details_raw:
                try:
                    parsed = json.loads(details_raw)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    details = parsed
            ts_raw = r.get("ts")
            ts = datetime.fromisoformat(ts_raw) if isinstance(ts_raw, str) else datetime.now(UTC)
            actor_raw = r.get("actor_id")
            target_raw = r.get("target")
            id_raw = r["id"]
            action_raw = r["action"]
            out.append(
                AuditEntry(
                    id=int(id_raw) if isinstance(id_raw, int) else 0,
                    ts=ts,
                    action=str(action_raw),
                    actor_id=int(actor_raw) if isinstance(actor_raw, int) else None,
                    target=str(target_raw) if isinstance(target_raw, str) else None,
                    details=details,
                )
            )
        return out

    # ------------------------------------------------------------------ helpers

    async def _maybe_broadcast(
        self,
        entry_id: int,
        action: str,
        actor_id: int | None,
        target: str | None,
        details: dict[str, Any] | None,
    ) -> None:
        cfg = self._audit_cfg()
        stream = cfg.get("stream")
        topic = cfg.get("topic", "audit") or "audit"
        if not stream:
            return

        parts: list[str] = [f"**[audit #{entry_id}]** `{action}`"]
        if actor_id is not None:
            parts.append(f"actor=`{actor_id}`")
        if target:
            parts.append(f"target=`{target}`")
        if details:
            payload = json.dumps(details, sort_keys=True, default=str)
            # Keep broadcasts readable in a stream view; full payload
            # is always available in the DB row.
            if len(payload) > 500:
                payload = payload[:497] + "..."
            parts.append(f"details=`{payload}`")
        text = " ".join(parts)
        try:
            await self.client.send_stream_message(stream, topic, text)
        except Exception:
            logger.exception("audit broadcast failed for entry %s (action=%s)", entry_id, action)
