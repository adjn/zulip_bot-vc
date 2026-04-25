# GitHub Copilot Instructions

> The canonical agent guide is `AGENTS.md` at the repo root. Keep this file
> short and in sync; deeper rules live there.

## Project

Modular Zulip bot in Python (3.12+) using `zulip` SDK + `trio`. Features:
anonymous DM relay with scheduled deletion, watch-and-subscribe rule engine,
admin DM commands.

## Hard rules (do not violate)

- **Never block trio.** All blocking Zulip / file I/O goes through
  `trio.to_thread.run_sync(...)`. The long-poll path uses
  `abandon_on_cancel=True` so shutdown is prompt.
- **Drop self-authored events** in the dispatcher.
- **Default-disabled** for every feature in `DEFAULT_CONFIG`.
- **Anonymous content is untrusted.** Strip wildcard mentions, length-cap,
  and escape backticks before placing in a code fence.
- **Don't log full API responses or raw user content** at WARNING+.
- **Admin auth** is `is_admin || is_owner` plus a config allowlist for super-
  admin actions. Cache role lookups with a TTL.
- **No secrets in code, logs, or repo working directory.** CI writes
  `.zuliprc` to `$RUNNER_TEMP`.

## Architecture

- `core.dispatcher.FeatureHandler` is the feature contract.
- `core.client.ZulipTrioClient` wraps the SDK; tests use a fake matching the
  protocol shape in `tests/fakes.py`.
- `ConfigManager` deep-merges defaults and persists atomically.
- Scheduled deletions and pending confirmations are in-memory only today —
  durability work goes in `storage/` and is gated by tests.

## Style

PEP 8, type hints, docstrings on public APIs, `ruff` for lint+format, `mypy`
for type-checking, `pytest` + `pytest-trio` for tests.

## Don't

- Don't read HTTP headers off `zulip.Client` response dicts — the SDK
  discards them.
- Don't reintroduce `pylint`.
- Don't add a new feature without an accompanying test.
- Don't add a new feature without `enabled: False` in `DEFAULT_CONFIG`.
