# `admin_controls`

Admin DM commands. The bot only acts on `!`-prefixed direct messages
from a Zulip admin or owner; the role lookup is cached briefly to avoid
a `get_user_by_id` round-trip per command.

## Commands

| Command | Summary |
| --- | --- |
| `!help` | List every registered admin command, or show usage for one. Auto-generated from the registry; no per-feature edits needed. |
| `!config show` / `!config set <key> <value>` | View or live-edit `config.yaml`. Writes are atomic. Secret keys are redacted from `show` output (see `_REDACT_PATTERNS`). |
| `!anon set <field> <value>` | Live-edit the `anonymous_posting` block (e.g. `enabled`, `target_stream`, `delete_after_minutes`). |
| `!access add <stream> <topic> <phrase> <target_stream>` / `!access remove <index>` | Manage `private_access.watch_rules`. |
| `!subscribe <stream>` | Subscribe the bot to a public stream so it can receive its events. |

## Config keys

`admin.super_admin_user_ids: list[int]` (optional explicit allowlist),
`admin.role_cache_ttl_seconds: int` (role lookup cache TTL, default
`60`).

## Storage tables

None directly. Some commands write to other features' tables via their
config (e.g. scheduling cleanup).

## Watch points

- New commands plug into `_build_registry` — see `feature.py:130`.
- Role-cache TTL is per-process; restarts and reload reset it. A user
  who loses admin role keeps access until the TTL elapses.
- `!config show` redaction is regex-based — extending the redaction
  list is a one-line change at the top of `feature.py`.
