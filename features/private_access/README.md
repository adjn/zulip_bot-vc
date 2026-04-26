# `private_access`

Watches `(stream, topic)` for an exact (case- and
whitespace-insensitive) trigger phrase. On match the bot subscribes
the sender to a target stream and reacts with `:saluting_face:`.

## ⚠ Not access control

Anyone who learns the phrase can self-subscribe. This is a
low-friction self-serve mechanism, not a security boundary. A real
gate (admin-approved subscription, mod-review queue, etc.) is tracked
as a long-term roadmap item.

## Commands

No standalone commands. Operators manage rules via
`admin_controls`:

- `!access add <stream> <topic> <phrase> <target_stream>`
- `!access remove <index>`

(See the auto-generated `!help` output for current syntax.)

## Config keys (under `private_access:`)

```yaml
private_access:
  enabled: false
  watch_rules:
    - stream: access-requests
      topic: example-topic
      phrase: "open sesame"
      target_stream: private-room-1
```

## Storage tables

None. State (parsed `WatchRule`s) is rebuilt from `config.yaml` and
cached by `ConfigManager.version` — the cache invalidates on any
config update.

## Watch points

- Phrase comparison is normalized via `utils.matching.normalize_phrase`
  (case-fold + Unicode NFKC + whitespace collapse).
- The version-counter cache means malformed rules are warned about
  once per config load, not once per matching message.
- Subscribing the *bot* to the target stream is on the operator. The
  bot can't subscribe a user to a stream it isn't already in.
