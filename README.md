# zulip_bot-vc

A modular Zulip bot written in Python using the official `zulip` client and `trio`.  
Current features:

- **Anonymous posting** via DM (with confirmation and timed deletion of the posted message).
- **Private access**: watch specific streams/topics for trigger phrases and subscribe users to target streams, reacting with `:saluting_face:`.
- **Admin controls** via DM for updating YAML-based configuration at runtime.

By default, **all feature modules are disabled**. You must explicitly enable them in `config.yaml` or via admin commands (for now, `enabled` flags are edited in the file).

---

## Installation (I haven't reviewed if any of this is correct, do not follow)

1. **Clone or copy the project** to a machine that can reach your Zulip server (e.g., EC2, or for testing, GitHub Actions).

2. **Create a virtualenv** and install dependencies:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Create a Zulip bot user** in your organization and download its `.zuliprc` file.

   You can provide this to the bot in two main ways:

   - Place the file at `~/.zuliprc` (default Zulip location), or
   - Set `ZULIP_CONFIG_FILE=/path/to/your.zuliprc` in the environment, or
   - For GitHub Actions, set the secret `ZULIP_RC` to the **contents** of your `.zuliprc`; the workflow writes it into `.zuliprc` and sets `ZULIP_CONFIG_FILE` for you.

4. (Optional) Set config path via env:

   ```bash
   export ZULIP_BOT_CONFIG=/path/to/config.yaml
   ```

   If not set, `config.yaml` in the current directory will be used/created.

---

## Running the bot locally / on a server

From the project directory:

```bash
source .venv/bin/activate
python bot_main.py
```

This starts a long-running process that:

- Registers an event queue with your Zulip server.
- Listens for `message` events.
- Dispatches them to the configured features.

For production, run it under a process manager (systemd, supervisord, Docker, etc.).

---

## Running the bot via GitHub Actions (manual start)

A workflow is provided at [`.github/workflows/run-zulip-bot.yml`](.github/workflows/run-zulip-bot.yml). It is triggered manually using `workflow_dispatch`.

Key points:

- Uses **Python 3.13**.
- Reads your `.zuliprc` contents from the secret `ZULIP_RC`.
- Optionally reads a `config.yaml` from the secret `ZULIP_BOT_CONFIG`.

### Required secrets

In your repository settings, under **Secrets and variables → Actions**, define:

- `ZULIP_RC` – the full text of your bot’s `.zuliprc` file.
- (Optional) `ZULIP_BOT_CONFIG` – the full YAML contents you want as `config.yaml`.

When you manually run this workflow, the bot will start on a GitHub-hosted runner and keep running until the job is stopped or the runner is torn down. This is intended as a **proof-of-concept** or test environment—not a permanent production deployment.

---

## Configuration

Configuration is stored in YAML (default: `config.yaml`). If none exists, a **default one** is created automatically.

By default, **all feature modules are disabled**. You must set `enabled: true` under each module you want to use.

Example:

```yaml
anonymous_posting:
  enabled: false       # <- change to true to enable
  target_stream: anonymous
  target_topic: general
  delete_after_minutes: 10080  # 7 days (used when enabled)

private_access:
  enabled: false       # <- change to true to enable
  watch_rules:
    - stream: access-requests
      topic: example-topic
      phrase: "Default string 1"
      target_stream: private-room-1
    - stream: access-requests
      topic: example-topic
      phrase: "Default string 2"
      target_stream: private-room-2

logging:
  level: INFO
  anonymize_user_ids: true
```

You can edit this file directly or use admin commands (recommended for most updates once the bot is running, though `enabled` flags are currently file-only).

---

## Feature: Anonymous posting

**Status by default**: disabled (`anonymous_posting.enabled: false`).

To enable:

```yaml
anonymous_posting:
  enabled: true
  target_stream: anonymous
  target_topic: general
  delete_after_minutes: 10080
```

**Flow:**

1. A user sends a **DM** to the bot with their message.
2. The bot replies:

   > You wrote:
   >
   > ```text
   > <message preview>
   > ```
   >
   > Reply with `SEND` to post anonymously, or `CANCEL` to discard.

3. If the user replies `SEND`:
   - Bot posts `Anonymous message:\n\n<content>` to the configured stream/topic.
   - Schedules deletion of that posted message after `delete_after_minutes`.
   - Attempts to delete the original DMs (subject to organization policy).

4. If the user replies `CANCEL`:
   - Bot does **not** post.
   - Attempts to delete the original DMs.
   - Confirms cancellation.

5. If the user replies with anything else during the confirmation step:
   - Bot responds with:  
     `Unknown input. Please start over by sending your message again.`
   - The pending confirmation is cleared; the user can DM a new message to restart the flow.

**Notes:**

- Pending confirmations are stored **only in memory**. If the bot restarts, those pending states are lost.
- The scheduled deletions only store **message IDs and times**, not content.

---

## Feature: Private access

**Status by default**: disabled (`private_access.enabled: false`).

To enable, set `enabled: true` and define your watch rules:

```yaml
private_access:
  enabled: true
  watch_rules:
    - stream: access-requests
      topic: game-requests
      phrase: "I want to play a game"
      target_stream: game-room
```

**Behavior:**

- If a user posts in `stream: access-requests`, `topic: game-requests` with content exactly `"I want to play a game"` (ignoring leading/trailing spaces and case):
  - The bot subscribes that user to the stream `game-room`.
  - The bot reacts to the message with `:saluting_face:`.

Multiple rules can be specified in `watch_rules`.

Logs for this feature **do include** the numeric `sender_id` (as you requested):

```text
PrivateAccess: subscribing sender_id=42 to target_stream=game-room due to phrase match
```

---

## Admin controls

**Who is an admin?**

- The bot uses Zulip user metadata:
  - It treats users with `is_admin` or `is_owner` as admins.
- Admin commands must be sent as **DMs to the bot** and start with `!`.

These admin capabilities are **enabled by default** (no config flag to turn them off, only role-based checks).

**Common commands:**

### Subscribe bot to streams

```text
!subscribe <stream1> [stream2] [stream3] ...
```

Example:
```text
!subscribe general announcements anonymous
```

This subscribes the bot to the specified streams so it can monitor them and post messages. The bot will respond with confirmation of which streams were newly subscribed and which it was already subscribed to.

### Show config

```text
!config show
```

Bot responds with current config in YAML (in a code block).

### Configure anonymous posting

**Show current anonymous posting settings:**
```text
!anon show
```

**Update settings:**
```text
!anon set stream anonymous
!anon set topic general
!anon set delete_after_minutes 10080
```

The bot will confirm each change. You still need to set `anonymous_posting.enabled: true` in `config.yaml` (manually) or extend admin commands if you want to toggle `enabled` via commands in the future.

### Manage access rules

**Add a rule:**

```text
!access add
stream: access-requests
topic: game-requests
phrase: "I want to play a game"
target_stream: game-room
```

**Remove a rule:**

```text
!access remove
stream: access-requests
topic: game-requests
phrase: "I want to play a game"
```

The body after the first line is parsed as YAML.

---

## Privacy and logging

- For anonymous posting:
  - Logs avoid user emails and message contents and only use IDs where needed.
- For private access:
  - Logs do include `sender_id` and target stream name, as requested.

---

## Extending the bot

Features live in `features/` and implement a simple interface:

```python
from core.dispatcher import FeatureHandler
from core.models import MessageEvent

class MyNewFeature(FeatureHandler):
    async def handles(self, event: MessageEvent) -> bool:
        ...

    async def handle(self, event: MessageEvent) -> None:
        ...
```

Register the new feature in `bot_main.py`:

```python
from features.my_new_feature import MyNewFeature

my_feature = MyNewFeature(client, config_mgr, scheduler)
dispatcher.register_feature(my_feature)
```

This design lets you grow the bot function-by-function over time.
