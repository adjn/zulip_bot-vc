# Welcome DM feature

Listens for new users joining the realm (`realm_user` op=`add`) and DMs
them a configurable greeting after a configurable delay.

## Quick start

```yaml
welcome:
  enabled: true
  delay_minutes: 5
  message: |
    Hi! Welcome to the realm. :wave:
    Say hi in #general when you have a moment.
```

## Properties

* **Durable** — pending welcomes live in the SQLite `pending_welcomes`
  table; a bot restart between "user joined" and "delay elapsed" still
  delivers.
* **Idempotent scheduling** — `ON CONFLICT DO NOTHING` means a duplicate
  `realm_user.add` event (e.g. from a queue reconnect) doesn't duplicate
  the welcome.
* **At-most-once delivery** — `claim_due_welcomes` removes the row in the
  same transaction as the read, mirroring `DeletionScheduler`. A crash
  mid-DM drops the welcome rather than spamming.
* **Bots are filtered** in the dispatcher, not here — we never welcome
  ourselves, integrations, or other bots.

## Placeholders

The `message` template supports two substitutions:

| Placeholder | Replacement                       |
| ----------- | --------------------------------- |
| `{user_id}` | the new user's numeric id         |
| `{mention}` | a Zulip silent mention by user-id |

Unknown placeholders are tolerated (template sent verbatim) so a stray
`{` doesn't crash delivery.

## Watch points

* **Privacy** — we store `user_id` + `deliver_at`. No name, email, or
  message content. The audit log does **not** record welcome deliveries
  (low signal, high volume).
* **Throughput** — `tick()` runs every `Scheduler.poll_interval_seconds`
  (60s default). For a realm with bursty signups, raise that floor if
  the resulting batch DM rate trips Zulip's rate-limit.
* **Disable behaviour** — when `enabled` flips to false at runtime, the
  next tick *drains* pending rows rather than parking them. The opposite
  (queueing forever) would surprise admins who toggled the feature off
  to stop welcoming people.
