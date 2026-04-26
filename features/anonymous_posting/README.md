# `anonymous_posting`

A user DMs the bot; the bot replies with a sanitized preview and waits
for `SEND` or `CANCEL`. On `SEND`, the bot relays the content to a
configured stream/topic and schedules the relayed message for deletion.
The original DM and the pending state are persisted, so the privacy
contract (timed deletion) survives a restart.

## Commands

The user-facing surface is **DM-only and unprefixed**: any DM that
isn't a `!`-prefixed admin command and isn't `SEND` / `CANCEL` is
treated as a new submission. Operators tune behaviour through
`!anon set <field> <value>` from `admin_controls`.

## Config keys (under `anonymous_posting:`)

| Key | Default | Purpose |
| --- | --- | --- |
| `enabled` | `false` | Master switch (DM toggle: `!anon set enabled true`). |
| `target_stream` / `target_topic` | `"anonymous"` / `"general"` | Where relayed messages land. |
| `delete_after_minutes` | `10080` (7 days) | Auto-delete delay for relayed messages. |
| `max_content_length` | `4000` | Hard cap on submitted content. Over-cap submissions are rejected before the preview. |
| `min_seconds_between_posts` | `30` | Per-sender cooldown enforced via the `cooldowns` table. |
| `scrub_wildcard_mentions` | `true` | Neutralises `@all` / `@everyone` / `@stream` / `@topic` / wildcard role mentions so anonymous posts cannot mass-notify. |
| `pending_ttl_minutes` | `10` | Pending-confirmation TTL. After expiry the next DM is treated as a new submission, not as `SEND` / `CANCEL`. |

## Storage tables

- `pending_confirmations` — sender → submitted content, expires-at.
- `scheduled_deletions` — relayed message id → delete-at timestamp.
- `cooldowns` — sender → next-allowed-at timestamp.

## Watch points

- Wildcard scrub is a regex (`_scrub_wildcards` in `feature.py`) — add
  patterns there, not at call sites.
- Backticks in user content are escaped before they enter the
  preview's code fence (`_escape_for_code_fence`); breaking the fence
  would let users inject markdown into the admin-facing preview.
- The "anonymous" promise applies to the *posted* content: the bot
  knows the original DM sender, which is how cooldowns and any future
  ban-list / mod-review queue work.
