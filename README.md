# rssignal

An RSS feed reader for Signal.

rssignal parses RSS feeds and forwards items as Signal text messages. It sends
by shelling out to the [`signal-cli`](https://github.com/AsamK/signal-cli)
command-line tool, so messages come from your own linked Signal account.

> **Status:** early. Signal setup, message sending, one auto-created group per
> feed, feed parsing/sending, message templates, per-field filters, extracted
> fields, link previews, and send-once-only work.
> The cloud service (POST-triggered) comes next.

## Requirements

- Python 3.13+ (installs `feedparser` and `Pillow`)
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
     a note-to-self, or a `group:<id>` from `rssignal groups`. Optional, and not
     used by `run` — feeds send to the group named after them.

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
`--quiet` to print just the recipient values. The same `group:` value works with
`send --to`, `run --to`, and as `RSSIGNAL_RECIPIENT`. Feeds don't need one: they
find their group by name (see [One group per feed](#one-group-per-feed)).

### Creating a group

Feeds create their own group on the first send, so this is for making one ahead
of time — or for a group you just want to have:

```bash
rssignal create-group "Podcast drops" \
  --description "One episode a day" \
  --avatar ./artwork.jpg \
  --announcement
```

Name a feed after it and that feed will send there. `--avatar` sets the group
image and takes a **local file path**, not a URL. `--announcement` restricts
sending to admins, which suits a group that exists to receive a feed.

**The new group contains only you.** rssignal has no way to add anyone else —
there is no `--member` flag, and nothing here reads your contacts. Invite people
from Signal on your phone once the group exists; that keeps the decision about
who joins a group where it belongs.

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
| `name`              | yes      | The Signal group this feed sends to. Also `{feed_name}` in templates.  |
| `message_template`  | no       | Message text with `{field}` placeholders. Omit for the built-in layout. |
| `extract`           | no       | Define new fields by regex against existing ones. See below.           |
| `link_preview`      | no       | Send a link preview card. Defaults to on for `podcast`, off for `regular`. |
| `preview_url`       | no       | Template for the card's link. Defaults to `{link}`.                    |
| `preview_title`     | no       | Template for the card's title. Defaults to `{title}`.                  |
| `preview_description` | no     | Template for the card's text. Omit for a card with no description.     |
| `<field>_contains`  | no       | Keep items whose field contains any of these terms.                    |
| `<field>_excludes`  | no       | Drop items whose field contains any of these terms.                    |
| `<field>_matches`   | no       | Keep items whose field matches any of these regexes.                   |

Unknown keys are rejected, so a typo like `title_contain` is an error rather than a
silently ignored setting. There is no recency setting: see
[what rssignal remembers](#what-rssignal-remembers).

### One group per feed

A feed's `name` is the Signal group it sends to. There is no recipient to look up
or paste in: on the first send rssignal finds the group of that name, and creates
it if there isn't one.

```
$ rssignal run
Created group 'Podcast drops' for this feed.
3 item(s) sent.
```

A created group holds **only you** and is announcement-only, so it stays a feed
rather than becoming a chat. It also takes the feed's own identity: the channel
artwork (`<itunes:image>` for a podcast, the site's logo for a blog) as its
picture, and the channel's description as its group description. Both are
channel-level — an episode's own image and notes stay on the message, where they
belong. Invite people from Signal on your phone; rssignal never adds anyone.

Those settings are applied **only when rssignal creates the group.** A group you
already had is used exactly as it is — rssignal will not restyle a group you made
yourself. To set them yourself:

```python
from rssignal import update_group
update_group("<group id>", description="A daily podcast.", avatar="./artwork.jpg")
```

Group pictures have two traps, both of which fail **silently** — the command
exits 0 and the group simply has no image:

- `updateGroup` ignores `--avatar` on the call that *creates* a group (though it
  does honour `--name` and `--set-permission-send-messages` there). rssignal
  therefore sets the picture in a second call against the new group id, and puts
  the description there too rather than trust it to the creating call.
- Signal drops an avatar that is too large. A 1400×1400 podcast cover vanishes;
  512×512 arrives. rssignal scales anything bigger down to 512px on its longest
  side before sending, keeping the aspect ratio.

Long feed descriptions are trimmed to 480 characters on a word boundary. If
setting either fails you get a warning on stderr and still keep the group.

> **Note:** deleting a group *chat* in the Signal app does not leave the group —
> it only removes the conversation from your list. rssignal still sees it and will
> keep sending there. Use **Leave group** if you want a feed to start over with a
> fresh one.

Every run drains the incoming message queue before reading the group list.
`listGroups` reads local state on a linked device, and that state lags: a group
you made on your phone is invisible until it syncs, and a group you *left* still
reads as active. Matching a group you left is the dangerous half — signal-cli
exits 0 sending into it, so the message is counted as sent and simply never
arrives. Refreshing up front costs one `signal-cli` call per run and removes the
whole class of problem.

Matching is case-insensitive and ignores surrounding whitespace. Groups you have
left or blocked don't count, so a feed named after one of those gets a fresh
group. If two groups share a name rssignal stops and asks you to rename one
rather than guess.

**Before the first real run, check `rssignal groups`.** If the group you mean is
named differently from the feed, rssignal will make a second one — rename the
group in Signal, or change the feed's `name`.

To try a real send without touching any group:

```bash
rssignal run --to +31611111111      # your own number; creates nothing
```

`RSSIGNAL_RECIPIENT` is the default for `rssignal send` only. `run` ignores it.

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
  image_url       https://example.com/artwork.jpg
  author          Example Media
  categories      news, politics
  feed_name       Some Podcast
  itunes_duration 00:42:11                        (extra)
  id              urn:uuid:8f2c…                  (extra)
  episode_id      54321                           (extra)
```

Anything you define with `extract` shows up here too, which is the quickest way to check
a pattern actually matches.

Add `--template` to preview a message against that item without sending anything:

```bash
rssignal fields --feed "Some Podcast" --template "🎧 {title} — {itunes_duration}"
```

### Extracting fields

Feeds often bury the one value you want inside another. Podcasts in particular
frequently have no episode webpage in the feed at all — the episode's id exists only
inside the audio URL. `extract` defines a new field by running a regex over an existing
one:

```json
"extract": {
  "episode_id": { "from": "link", "pattern": "/file/[^/]+/(\\d+)/" }
},
"preview_url": "https://example.com/listen/{episode_id}"
```

The new field takes the first capture group, or the whole match when the pattern has no
groups. From there it behaves like any other field: usable in `message_template`, in the
`preview_*` templates, as a filter target, and listed by `rssignal fields`. Remember JSON
needs backslashes doubled (`\\d`, not `\d`).

Rules read the item as it came off the feed, so they can't chain into one another, and an
extracted name can never shadow a built-in field like `title`. A pattern that doesn't
match yields an empty field rather than an error — check yours with `rssignal fields`
before relying on it.

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
multiple filters must **all** pass. Filters are applied before the watermark, so
an item you filter out never counts as sent.

Check what would be sent, then send for real:

```bash
rssignal run --dry-run          # prints matching items; sends and creates nothing
rssignal run --to +31611111111  # real send, to you instead of the feeds' groups
rssignal run                    # sends to each feed's group, creating it if needed
rssignal run --config other.json
```

`--dry-run` also tells you which group each feed resolved to, and whether it
would have to be created — worth reading before the first real run. It refreshes
the group list like a real run does, so its answer is the one a real run gets.

For `podcast` feeds the episode's audio enclosure is downloaded and sent with
`signal-cli --voice-note`. Depending on the file's codec, Signal may show it as
a regular audio attachment rather than an in-app voice note.

### Link previews

Podcast items also carry a link preview card — the episode title and its artwork —
so an episode is recognizable next to its voice note. Set `"link_preview": false`
to turn it off, or `true` on a `regular` feed to turn it on.

**A podcast episode with a card arrives as two messages:** the text and the card
first, then the voice note on its own. Signal silently drops a preview card from
any message that also has an attachment, so they cannot be combined. A podcast
feed with `"link_preview": false` goes back to a single message.

The card and the body split the item between them rather than repeating it. The
card gets the title, so the built-in layout leaves it out of the body and sends
the description plus the URL. The card carries no description unless you ask for
one with `preview_description`. A `message_template` is never touched — if you
wrote the layout, you decide what is in it.

The card's artwork comes from `image_url`: the episode's own `<itunes:image>` or
`<media:thumbnail>`. The show's channel artwork is deliberately *not* a fallback —
the same logo on every episode tells you nothing — so an episode without its own
image gets a card without one. Artwork is downloaded per item; if that download
fails the episode is still sent, just without the image.

Signal requires the previewed URL to appear in the message body, so rssignal
appends it if your template doesn't already include it. Many podcast feeds set
each episode's `link` to the raw `.mp3`; combine `extract` with `preview_url` to
point the card at the real episode page:

```json
"extract": { "episode_id": { "from": "link", "pattern": "/file/[^/]+/(\\d+)/" } },
"preview_url": "https://example.com/listen/{episode_id}",
"preview_title": "🎧 {title} ({itunes_duration})"
```

All three `preview_*` keys take the same `{field}` placeholders as
`message_template`. An item whose preview URL or title renders empty is sent
without a card rather than failing.

## What rssignal remembers

rssignal sends each item once. To do that it has to remember how far each feed
got — and it keeps that **in the feed's own Signal group description**, not in a
state file:

```
Two people argue about films they have not seen. New episode every Thursday.

[rssignal 2026-07-23T10:03:00+00:00]
```

The stamp is the publication date of the newest item that went out. On the next
run, anything published after it is sent, oldest first, and the marker moves.

There is nowhere better. `signal-cli` cannot read back messages it has sent — the
server hands each message over once, and a linked secondary device never receives
its own sends — and of everything `listGroups` reports, the description is the
only free-text field that is writable, readable back, and stored server-side. The
upside is that the record lives with the group: reinstall signal-cli, or link a
new machine, and nothing is resent.

**The cost:** Signal shows a group-detail change in the chat, so every run that
sends something also leaves one *"You changed the group description"* line in that
group. Runs with nothing new write nothing and stay completely silent.

Worth knowing:

- **A feed with no marker yet sends exactly one item** — the newest. A new group
  doesn't open with the whole back catalogue.
- **Items with no publication date are never sent.** There is no way to tell
  whether they are new.
- **Your own edits survive.** Rewrite a group's description in Signal and rssignal
  moves the marker around your text instead of pasting the feed's blurb back over
  it. Delete the marker and the feed's newest item is sent once more.
- **`--to` touches no group**, so it neither reads nor moves any marker. It sends
  the newest item, every time.
- **`--since` replays**, ignoring what the groups remember:

  ```bash
  rssignal run --since 2026-07-01                # everything published since then
  rssignal run --since 2026-07-01T09:00+02:00    # or to the minute
  ```

  The marker still ends up on the newest item actually sent, so a replay leaves it
  correct rather than rewound.
- **A send that fails part-way is safe.** Items go out oldest first and the marker
  lands on the last one that made it; the rest are retried on the next run.

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
