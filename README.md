# rssignal

An RSS feed reader for Signal.

rssignal parses RSS feeds and forwards items as Signal text messages. It sends
by shelling out to the [`signal-cli`](https://github.com/AsamK/signal-cli)
command-line tool, so messages come from your own linked Signal account.

> **Status:** early. Signal setup, message sending, feed parsing/sending, message
> templates, and per-field filters work. De-duplication and the cloud service
> (POST-triggered) come next.

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
     a note-to-self, or a `group:<id>` from `rssignal groups`.

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

### Sending to a group

A recipient is either an E.164 number or a group, written `group:<id>`. Signal's
group ids are base64 blobs, so `rssignal groups` lists them next to their names:

```bash
$ rssignal groups
  Book club       group:AAAA1111bbbb+cccc/dddd2222eeee3333ffff4444g=
  Weekend plans   group:BBBB2222cccc/dddd+eeee3333ffff4444gggg5555h=

$ rssignal send "hi all" --to "group:AAAA1111bbbb+cccc/dddd2222eeee3333ffff4444g="
```

Quote the value: group ids contain `+` and `/`, and may end in `=`.

**A group you just created won't appear yet.** rssignal runs as a linked
secondary device, so it only learns about new groups from sync messages waiting
in the incoming queue, and `listGroups` reads local state. Drain the queue first:

```bash
rssignal groups --refresh
```

Groups you have left or blocked are hidden; pass `--all` to see them, or
`--quiet` to print just the recipient values. The same `group:` value works as a
feed's `recipient` in `feeds.json` and as `RSSIGNAL_RECIPIENT`.

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

| Key                 | Required | Description                                                            |
| ------------------- | -------- | ---------------------------------------------------------------------- |
| `url`               | yes      | The RSS/Atom feed URL.                                                  |
| `type`              | yes      | `regular` (title + description + link) or `podcast` (audio voice note). |
| `name`              | no       | Label used in logs / dry-run output, and available as `{feed_name}`.    |
| `recipient`         | no       | Per-feed number or `group:<id>`; falls back to `RSSIGNAL_RECIPIENT`.    |
| `max_age_hours`     | no       | Only send items published within this many hours.                      |
| `max_age_days`      | no       | Added to `max_age_hours`. Omit both to send every item in the feed.    |
| `message_template`  | no       | Message text with `{field}` placeholders. Omit for the built-in layout. |
| `<field>_contains`  | no       | Keep items whose field contains any of these terms.                    |
| `<field>_excludes`  | no       | Drop items whose field contains any of these terms.                    |
| `<field>_matches`   | no       | Keep items whose field matches any of these regexes.                   |

Unknown keys are rejected, so a typo like `max_age_hour` is an error rather than a
silently ignored setting.

### Message templates

`message_template` is filled in with `{field}` placeholders — the same fields the
filters use:

```json
"message_template": "🎧 {title} ({published_date})\n\n{description}"
```

A placeholder the item doesn't have renders as empty text rather than failing the
run, and the resulting blank gap is collapsed. Without a `message_template`, a
`regular` feed sends title + description + link and a `podcast` feed sends title +
description.

### Discovering fields

Which fields exist depends on the feed — most publish more than the standard few.
The `fields` command samples a real item and lists what you can use:

```bash
rssignal fields                                  # first feed in feeds.json
rssignal fields --feed "Some Podcast"            # by name or URL
rssignal fields --url https://example.com/rss    # not yet in the config
rssignal fields --item 3                         # sample the 3rd item
```

```
Fields for Some Podcast (item 1 of 25):

  title           Episode 402: the interview
  description     In this episode we talk to …
  link            https://example.com/402
  published       2026-07-22T06:00:00+00:00
  published_date  2026-07-22
  enclosure_url   https://example.com/402.mp3
  enclosure_type  audio/mpeg
  author          Example Media
  categories      news, politics
  feed_name       Some Podcast
  itunes_duration 00:42:11                        (extra)
  id              urn:uuid:8f2c…                  (extra)
```

Add `--template` to preview a message against that item without sending anything:

```bash
rssignal fields --feed "Some Podcast" --template "🎧 {title} — {itunes_duration}"
```

### Filters

Filters test a single field and are written as `<field>_<op>`, where the field is any
name from `rssignal fields`:

```json
"title_contains": ["interview", "special"],
"description_excludes": "rerun",
"title_matches": "^Episode \\d+"
```

Each takes a string or a list. `contains` and `matches` keep an item when **any**
value hits; `excludes` keeps it when **none** do. Matching is case-insensitive, and
multiple filters must **all** pass. Recency (`max_age_*`) is applied first.

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
