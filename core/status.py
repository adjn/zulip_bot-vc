"""Diagnostic status report for the bot.

Why a separate module
---------------------

The ``!status`` admin command needs to gather facts from several
places: storage (pending deletions), config (audit + admin sections),
and the runtime ctx (uptime). Putting that gathering in
:mod:`features.admin_controls` would conflate "render YAML config"
with "compute uptime" and make either hard to test on its own.

:class:`StatusReport` is a plain data carrier: pure functions on the
ctx + storage, no I/O of its own beyond a single ``pending_deletion_count``
read. The admin handler is then a one-liner that calls
:func:`StatusReport.gather` and renders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from core.context import FeatureContext
from storage.db import SCHEMA_VERSION


@dataclass(frozen=True)
class StatusReport:
    """Snapshot of bot runtime + config diagnostics."""

    uptime: timedelta | None
    schema_version: int
    pending_deletions: int | None
    audit_enabled: bool
    audit_broadcast_stream: str | None
    super_admin_count: int
    bot_user_id: int | None

    @classmethod
    async def gather(cls, ctx: FeatureContext) -> StatusReport:
        """Collect a fresh status snapshot from the shared context."""
        now = datetime.now(UTC)
        uptime = (now - ctx.started_at) if ctx.started_at is not None else None

        pending: int | None = None
        if ctx.storage is not None:
            pending = await ctx.storage.pending_deletion_count()

        cfg = ctx.config_mgr.get()
        audit_cfg = cfg.get("audit", {}) or {}
        admin_cfg = cfg.get("admin", {}) or {}

        return cls(
            uptime=uptime,
            schema_version=SCHEMA_VERSION,
            pending_deletions=pending,
            audit_enabled=bool(audit_cfg.get("enabled", True)),
            audit_broadcast_stream=audit_cfg.get("stream"),
            super_admin_count=len(admin_cfg.get("super_admin_user_ids", []) or []),
            bot_user_id=ctx.bot_user_id,
        )

    def render(self) -> str:
        """Format the snapshot as a Markdown bullet list for DM reply."""
        lines: list[str] = ["**Bot status:**"]
        lines.append(f"- Uptime: {_fmt_uptime(self.uptime)}")
        lines.append(f"- DB schema version: `{self.schema_version}`")
        lines.append(
            f"- Scheduled deletions pending: "
            f"{self.pending_deletions if self.pending_deletions is not None else 'n/a'}"
        )
        lines.append(f"- Audit log enabled: `{self.audit_enabled}`")
        lines.append(
            f"- Audit broadcast: "
            f"{f'`{self.audit_broadcast_stream}`' if self.audit_broadcast_stream else '`disabled`'}"
        )
        lines.append(f"- Super-admin allowlist: `{self.super_admin_count}` user(s)")
        lines.append(
            f"- Bot user id: `{self.bot_user_id}`"
            if self.bot_user_id is not None
            else "- Bot user id: `unknown`"
        )
        return "\n".join(lines)


def _fmt_uptime(uptime: timedelta | None) -> str:
    """Render a :class:`timedelta` in a human-friendly way (``2d 3h 4m``)."""
    if uptime is None:
        return "unknown (no start time recorded)"
    total = int(uptime.total_seconds())
    # Clamp at zero — clock going backwards or started_at in the future
    # shouldn't crash the renderer; the operator just sees 0s uptime.
    total = max(total, 0)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)
