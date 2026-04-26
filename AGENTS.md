# Agent guide for `zulip_bot-vc`

This file is the canonical guide for any coding agent (Copilot, Claude, Codex,
etc.) working in this repo. The `.github/copilot-instructions.md` file mirrors
the same guidance for GitHub Copilot.

## What this is

A modular Zulip bot in Python (3.12+) using the official `zulip` SDK and
`trio`. Three features today:

- `features/anonymous_posting.py` — DM → confirmation → relay to a stream,
  with a persistent schedule (SQLite) to delete the relayed message later.
- `features/private_access.py` — watch `(stream, topic)` for an exact phrase,
  auto-subscribe the sender to a target stream and react.
- `features/admin_controls.py` — DM-only `!`-prefixed admin commands.

`bot_main.py` is the entry point. `core/` is the dispatch + Zulip client
wrapper. `storage/` holds the SQLite-backed `Storage` (`storage/db.py`)
and the YAML config store (`storage/file_store.py`). `utils/` is a thin
helper layer. `tests/` uses `pytest` + `pytest-trio` with a fake client;
storage tests run against an in-memory SQLite (`:memory:`) for speed,
production uses an on-disk DB at `./data/bot.db` by default.

## Hard rules — do not violate

1. **Never block the trio event loop.** Every blocking Zulip / file I/O call
   must go through `trio.to_thread.run_sync(...)`. Long-poll thread calls
   should pass `abandon_on_cancel=True` so shutdown is prompt.
2. **Never log full API responses or raw user message content** at WARNING or
   higher. Log `code` + `msg` only. User content can be DEBUG-only and gated.
3. **Default-disabled.** Any new feature ships `enabled: False` in
   `DEFAULT_CONFIG`. The bot must do nothing on first run beyond replying to
   admin pings.
4. **Drop self-authored events** in the dispatcher. Never react to events
   whose `sender_id` is the bot's own user id.
5. **Anonymous content is untrusted input.** Strip wildcard mentions
   (`@**all**`, `@*everyone*`, `@_*…*_`, `@**channel**`, etc.), enforce a
   length cap before storing or posting, and escape backticks before placing
   in a code fence.
6. **Admin commands** are gated on `is_admin || is_owner` *and* (for super-
   admin operations) an explicit allowlist in config. Cache the role lookup
   with a TTL — do not round-trip `get_user_by_id` per command.
7. **Durable user-visible state lives in `Storage`.** Anything that
   embodies a privacy or rate-limit promise (scheduled deletions,
   pending confirmations, cooldowns) must round-trip through
   `storage/db.py`, not a process-local dict. The bot fails closed at
   startup if the DB path isn't writable — don't paper over that with
   an in-memory fallback.
8. **No secrets in code, in logs, or in the repo working directory.** The
   workflow writes `.zuliprc` to `$RUNNER_TEMP`, not the checkout.

## Architecture rules

- Features implement `core.dispatcher.FeatureHandler` (`async handles`,
  `async handle`).
- The dispatcher catches per-feature exceptions and logs them — but if you
  see repeated exceptions from one feature, fix the feature; do not rely on
  the catch.
- Features depend on `core.client.ZulipTrioClient` *via the protocol shape*
  used in tests (`tests/fakes.py`). Don't reach around the client to call the
  raw `zulip.Client`.
- Config is read via `ConfigManager.get()`. Mutating config goes through
  `ConfigManager.update(new_cfg)` which deep-merges, schema-checks, and
  atomically writes.

## Storage

- Durable state lives in a single SQLite file (`./data/bot.db` by
  default; override with `BOT_DB_PATH` or `storage.db_path`).
- `storage/db.py` exposes a `Storage` class. All public methods are
  `async` and dispatch SQLite work to a worker thread via
  `trio.to_thread.run_sync`, serialised by an internal `trio.Lock`.
- WAL mode is on; timestamps are stored as ISO-8601 UTC text.
- Schema migrations live in `_apply_migrations()` and are idempotent;
  bump `SCHEMA_VERSION` and add a numbered block when changing schema.
  The bot refuses to start if the on-disk version is newer than the
  code's `SCHEMA_VERSION`.
- Tests use `:memory:` (or `tmp_path` for restart-survival tests). Don't
  introduce a separate `FakeStorage` — the real implementation is fast
  and tests against real SQL behaviour.

## Adding admin commands

Admin commands live behind `core.commands.CommandRegistry` in
`features/admin_controls.py`. To add a new one:

1. Write an `async def _handle_xxx(self, ctx: CommandContext) -> None`
   method on `AdminControlsFeature`. It receives the parsed `tokens`
   and (for YAML-bodied commands) the `body`. It owns its own user
   reply via `self.client.send_private_message`.
2. Register it in `_build_registry()` with a `Command(name, summary,
   usage, handler)`. Use `!command` (with the leading bang) as `name`.
3. That's it — `!help`, `!help <command>`, and the "unknown command"
   reply pick it up automatically. On argument errors, call
   `self._send_usage(ctx, "!command")` for a consistently formatted
   reply.

The registry is *exact-match* on the first whitespace token. There is
no `startswith` fallback; `!configfoo` will not route to `!config`.

## Code style

- PEP 8 + ruff defaults (`pyproject.toml`).
- Type hints on every function signature.
- Docstrings on public classes and public functions; one-liners are fine for
  small helpers.
- Prefer `dataclasses` for plain records, `typing.Protocol` for interfaces
  used by tests.
- No `print()` — use `logger`.

## Comment policy

This is a learning project as well as a working bot, so we comment a bit
more than a typical production codebase would. Three tiers:

1. **Module docstring** (always). 2-4 lines at the top of every file
   describing *what role this file plays*, not how. The first thing a
   reader sees.
2. **Section signposts** in any file with more than two logical groups of
   functions. Single-line dividers like `# --- event queue ---` so the
   file is skimmable. See `core/client.py` for the canonical example.
3. **"Why" comments** at any spot where a reader would predictably ask
   "why?" or, worse, would "fix" the code and break something subtle.
   Concrete signals you need one:
   - The line *looks* like dead code, a typo, or a redundant wrapper, but
     isn't (e.g. the `current_last: int = current_last` default-arg in
     `core/client.py`, or `_call_with_retries`' final unreachable
     `return`).
   - A flag or argument that *seems* optional but isn't (e.g.
     `abandon_on_cancel=True`).
   - State that is held in memory only, with implications for restart
     behaviour or invalidation (e.g. the role-cache TTL in
     `admin_controls.py`, the per-feature config-version cache in
     `private_access.py`).
   - A defensive check whose threat model isn't obvious from the check
     itself.

   Keep these short — usually 1-5 lines. Comment the reason, not the code.

What we **don't** add:

- `# loop over events` next to `for event in events:`.
- Restating type annotations in prose.
- Commit-message-style block comments above functions (those belong in
  `git log` or in a docstring).
- Personal narration ("I tried X but it didn't work, so…").

When in doubt, lean toward *adding* a comment for tier-3 cases — it's
cheaper to read past a slightly redundant comment than to re-derive a
non-obvious invariant from the code.

## How to run things

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

# Lint & format
ruff check .
ruff format .

# Type check
mypy .

# Tests
pytest

# Run the bot (needs ~/.zuliprc or ZULIP_CONFIG_FILE)
python bot_main.py
```

## When you change behavior

1. Update `tests/` first if the change is testable.
2. Update the README user-facing sections if behavior changes.
3. Update `DEFAULT_CONFIG` in `config.py` if you add a config key (and add a
   test that the default round-trips).
4. Keep `enabled: False` for any new feature module.

## What NOT to do

- Don't add a new top-level dependency without weighing whether a stdlib
  approach works.
- Don't reintroduce `pylint` — `ruff` covers what we need.
- Don't add `print(...)` debugging in committed code.
- Don't catch `Exception` and swallow it silently outside the dispatcher's
  per-feature isolation.
- Don't read HTTP response headers off the dict returned by `zulip.Client` —
  the SDK doesn't surface them. If you need rate-limit telemetry, subclass
  `zulip.Client` and capture `response.headers` in the transport layer.
