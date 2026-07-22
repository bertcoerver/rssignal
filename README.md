# rssignal

An RSS feed reader for Signal.

rssignal parses RSS feeds and forwards items as Signal text messages. It sends
by shelling out to the [`signal-cli`](https://github.com/AsamK/signal-cli)
command-line tool, so messages come from your own linked Signal account.

> **Status:** early. Signal setup, message sending, and feed parsing/sending
> work. De-duplication and the cloud service (POST-triggered) come next.

## Requirements

- Python 3.13+
- `signal-cli` on your PATH:
  ```bash
  brew install signal-cli        # macOS
  ```
- Optional, for a scannable QR code during linking:
  ```bash
  brew install qrencode
  ```

signal-cli stores its account state in `~/.local/share/signal-cli` by default.

## Installation

```bash
pip install -e ".[dev]"
```

## Setup

1. Copy the example config and fill in your numbers (the file is git-ignored):
   ```bash
   cp .env.example .env
   ```
   - `RSSIGNAL_ACCOUNT` — the E.164 number rssignal sends from (the account you
     link below).
   - `RSSIGNAL_RECIPIENT` — default recipient for `send`; use your own number for
     a note-to-self.

2. Link this machine to your Signal account (acts like Signal Desktop):
   ```bash
   rssignal link
   ```
   Scan the QR code from Signal on your phone: **Settings → Linked Devices →
   Link New Device**. The command blocks until linking and the initial sync
   finish.

3. Check everything is wired up:
   ```bash
   rssignal doctor
   ```

## Usage

Send a message from the command line:

```bash
rssignal send "hello from rssignal"
rssignal send "to someone else" --to +31611111111
```

Or from Python:

```python
from rssignal import send_msg

send_msg("hello from rssignal")                       # uses configured recipient
send_msg("hi there", recipient="+31611111111")        # explicit recipient
```

## Feeds

Describe the feeds to follow in a JSON config (git-ignored):

```bash
cp feeds.example.json feeds.json
```

Each feed entry supports:

| Key             | Required | Description                                                            |
| --------------- | -------- | ---------------------------------------------------------------------- |
| `url`           | yes      | The RSS/Atom feed URL.                                                  |
| `type`          | yes      | `regular` (title + description + link) or `podcast` (audio voice note). |
| `name`          | no       | Label used in logs / dry-run output.                                   |
| `recipient`     | no       | Per-feed recipient; falls back to `RSSIGNAL_RECIPIENT`.                 |
| `max_age_hours` | no       | Only send items published within this many hours.                      |
| `max_age_days`  | no       | Added to `max_age_hours`. Omit both to send every item in the feed.    |

Check what would be sent, then send for real:

```bash
rssignal run --dry-run          # prints matching items, sends nothing
rssignal run                    # sends one Signal message per item
rssignal run --config other.json
```

For `podcast` feeds the episode's audio enclosure is downloaded and sent with
`signal-cli --voice-note`. Depending on the file's codec, Signal may show it as
a regular audio attachment rather than an in-app voice note.

> **Note:** there is no de-duplication yet — running again while items are still
> inside their `max_age` window resends them. Seen-tracking is the next milestone.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The test suite mocks all `signal-cli` calls, so it never sends real messages.

> **Note:** each message currently uses a one-shot `signal-cli send`. For the
> cloud service, running signal-cli as a long-lived JSON-RPC daemon would avoid
> per-message JVM startup cost — a future optimization.

## Release

```bash
./tag_release.sh patch   # or minor / major
```
