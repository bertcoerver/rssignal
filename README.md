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
- Optional, only for feeds whose items link to a video (see
  [video items](#video-items)):
  ```bash
  brew install ffmpeg
  ```
- Optional, only for YouTube feeds:
  ```bash
  pip install rssignal[video]   # yt-dlp
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
| `url`               | yes      | The RSS/Atom feed URL, or an ARTE collection page, or a YouTube channel. See [video items](#video-items). |
| `name`              | yes      | The Signal group this feed sends to. Also `{feed_name}` in templates.  |
| `message_template`  | no       | Message text with `{field}` placeholders. Omit for the built-in layout. |
| `extract`           | no       | Define new fields by regex against existing ones. See below.           |
| `link_preview`      | no       | Send a link preview card. Defaults to on for items with audio, off for the rest. |
| `preview_url`       | no       | Template for the card's link. Defaults to `{link}`.                    |
| `preview_title`     | no       | Template for the card's title. Defaults to `{title}`.                  |
| `preview_description` | no     | Template for the card's text. Omit for a card with no description.     |
| `<field>_contains`  | no       | Keep items whose field contains any of these terms.                    |
| `<field>_excludes`  | no       | Drop items whose field contains any of these terms.                    |
| `<field>_matches`   | no       | Keep items whose field matches any of these regexes.                   |

Unknown keys are rejected, so a typo like `title_contain` is an error rather than a
silently ignored setting. There is no recency setting: see
[what rssignal remembers](#what-rssignal-remembers).

There is no setting for what kind of feed it is, either. rssignal decides that per
*item*: one whose enclosure is audio goes out as a voice note with a preview card,
one that links to a video it can fetch goes out with the video attached, anything
else as text plus its link. This is checked per item rather than per feed on
purpose — a podcast that occasionally posts a written note gets that note as a
readable message with a working link, instead of a linkless stub. If you disagree
about the card, `link_preview` overrides it either way.

### Video items

Some feeds are about video but carry none: the item links to a player page and the
video lives somewhere else entirely. rssignal recognises two kinds of link and
attaches the video to the message — **ARTE** programmes
(`https://www.arte.tv/<lang>/videos/<programme-id>/…`) and **YouTube** videos
(`watch?v=…`, `youtu.be/…`, `/shorts/…`). Nothing to configure; the link in the
item is enough.

Both can also be followed without finding a feed first.

#### ARTE

ARTE publishes no RSS, so you don't need one either: point `url` straight at a
show's collection page and rssignal reads its episodes directly.

```json
{
  "name": "Le Dessous des Images",
  "url": "https://www.arte.tv/fr/videos/RC-023176/le-dessous-des-images/"
}
```

This is worth preferring over a third-party feed generator pointed at the same
page. The generator scrapes what the page renders — the site's furniture instead of
the synopsis — and stamps every item with the moment it scraped rather than when
the episode aired, which is the one thing rssignal needs to tell new from old.
Read directly, each episode arrives with its own title, its real synopsis, its
artwork, and the date it became available.

A collection lists the show's entire back catalogue, which has no dates attached;
rssignal looks up the newest dozen individually and leaves the rest undated, so
they are never sent. That is deliberate — a new group shouldn't open with a hundred
videos — and it costs a handful of small requests per run rather than one per
episode.

#### YouTube

Point `url` at a channel — any of the forms YouTube hands out:

```json
{
  "name": "Veritasium",
  "url": "https://www.youtube.com/@veritasium"
}
```

No API key is involved. YouTube publishes an Atom feed for every channel, at an
address nobody would guess, and rssignal swaps the channel page for it — so a
YouTube feed is a perfectly ordinary feed and filters, templates and extracts all
work on it. A `/channel/UC…` url is rewritten on the spot; the `@handle`, `/c/` and
`/user/` forms cost one small yt-dlp call to find the channel's id.

That feed carries the **last 15 uploads**, which is plenty to follow a channel and
no use for backfilling one.

The one thing it doesn't say is how long a video is, which is exactly what a
channel mixing clips with full episodes has to be filtered on — so rssignal asks
yt-dlp for the channel's listing and puts a `duration_seconds` field on each item.
That's one small request per feed, not one per video, and it makes a length filter
ordinary:

```json
{
  "name": "The Daily Show",
  "url": "https://www.youtube.com/@TheDailyShow",
  "duration_seconds_min": "7:00",
  "duration_seconds_max": "30:00",
  "published_weekday_contains": ["Tuesday", "Wednesday", "Thursday", "Friday"]
}
```

An upload the listing doesn't cover — a Short, which lives on its own tab — gets no
duration, and a length filter therefore drops it. Without yt-dlp, no item gets one:
that's a warning on stderr, and a length filter then keeps nothing.

Downloads need **yt-dlp** (`pip install rssignal[video]`), which in turn uses
ffmpeg. Uploads shorter than 90 seconds are treated as Shorts and dropped
entirely — no message, and they aren't reconsidered on the next run.

#### When YouTube asks you to confirm you're not a bot

```
[The Daily Show] video skipped: yt-dlp failed: ERROR: [youtube] 14j9b34VCJI:
Sign in to confirm you're not a bot.
```

YouTube blocks requests it can't attribute to a signed-in session, and gets
stricter about it the more a machine asks — so this tends to show up on a server,
on a VPN, or after a few runs in a row. The only thing it accepts is real cookies
from an account. Two ways to hand them over, both off by default:

```bash
# Read them out of a browser you are logged into, by name:
RSSIGNAL_YOUTUBE_COOKIES_FROM_BROWSER=firefox

# …or point at a cookies.txt export:
RSSIGNAL_YOUTUBE_COOKIES=~/.config/rssignal/youtube-cookies.txt
```

Either goes in your environment or `.env`, and applies to every yt-dlp call —
the downloads and the listing that fills in durations, which YouTube challenges
the same way. If both are set the file wins.

A profile can be named where there are several: `firefox:default-release`. Firefox
is the path of least resistance on macOS; Chrome and Safari encrypt their cookie
stores and will prompt for keychain access, which is no use from a cron job. On a
headless server there is no browser to read at all — export a cookies.txt from
your desktop (a Netscape-format cookie extension does this) and copy it over.

Use a throwaway Google account rather than your own: the cookies are a live
session, so the file is a credential — keep it out of the repo, and expect to
refresh it every few months when the session expires.

#### Quality, and what happens when it doesn't fit

This needs **ffmpeg** on your PATH (`brew install ffmpeg`). Only video feeds do, so
it stays optional — `rssignal doctor` reports whether it and yt-dlp are there
without failing.

Both sources offer the same video at several qualities and Signal refuses an
attachment over 100 MB, so rssignal picks the sharpest one it expects to fit,
from the reported file sizes where there are any and from bitrate times duration
where there aren't. A ten-minute ARTE episode typically arrives at 640x360, a
ten-minute YouTube video at 720p, and a long one a rung or two lower. Streams are
copied, never re-encoded, so this costs bandwidth and seconds rather than minutes
of CPU. Check what an item would arrive at before sending anything:

```bash
rssignal run --dry-run
# [Le Dessous des Images] -> group:Le Dessous des Images=: La bataille du drapeau
#     video: 640x360, ~63 MB
```

If the video can't be had — expired rights, a video too long for any quality to fit,
ffmpeg or yt-dlp missing — that's a warning on stderr, not a failure: the item still
goes out as text and its link. A readable message beats no message, and the item
isn't retried. Expect this for anything much over half an hour: 95 MB is 95 MB.

Unlike a voice note, a video goes out as a single message: these items get no
preview card by default, so there's nothing for the attachment to displace.

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

It also starts with **disappearing messages set to one week**
(`GROUP_EXPIRATION_SECONDS`). A feed group is a stream, not an archive: left
alone it grows without limit, and the phone holding it keeps every episode's
audio forever. This costs rssignal nothing — how far a feed got is kept in the
group *description*, which is metadata rather than a message and never expires,
so a group can empty itself completely and the next run still knows exactly
where it was. The "changed the group description" lines each sending run leaves
behind expire along with everything else. Note that the episodes go too, audio
included, so if a feed group doubles as your listening queue, turn it off.

Those settings are applied **only when rssignal creates the group.** A group you
already had is used exactly as it is — rssignal will not restyle a group you made
yourself, or change its expiry. To set them yourself:

```python
from rssignal import update_group
update_group("<group id>", description="A daily podcast.", avatar="./artwork.jpg")
update_group("<group id>", expiration=604800)   # one week; 0 turns it off
```

Group pictures have two traps, both of which fail **silently** — the command
exits 0 and the group simply has no image:

- `updateGroup` ignores `--avatar` on the call that *creates* a group (though it
  does honour `--name` and `--set-permission-send-messages` there). rssignal
  therefore sets the picture in a second call against the new group id, and puts
  the description and the expiry there too rather than trust them to the
  creating call.
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
run, and the resulting blank gap is collapsed. Without a `message_template`, an item
sends title + description + link — or title + description for one carrying audio,
where the link is left out because the enclosure itself is attached.

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

  title              Episode 402: the interview
  description        In this episode we talk to …
  link               https://example.com/402
  published          2026-07-22T06:00:00+00:00
  published_date     2026-07-22
  published_weekday  Wednesday
  enclosure_url      https://example.com/402.mp3
  enclosure_type     audio/mpeg
  image_url          https://example.com/artwork.jpg
  author             Example Media
  categories         news, politics
  feed_name          Some Podcast
  itunes_duration    00:42:11                     (extra)
  id                 urn:uuid:8f2c…               (extra)
  episode_id         54321                        (extra)
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

A rule reads better against the field the feed writes to a formula. Titles are
written for people and vary; descriptions are often boilerplate, and boilerplate is
what a filter can hold on to. `"description_excludes": ["sits down with"]` drops a
show's guest interviews more reliably than any pattern over their titles, because
the phrase is the house style rather than an accident of the episode.

`matches` can also express an exclusion that `excludes` can't, since a negative
lookahead over the whole field keeps everything the pattern doesn't describe:

```json
"title_matches": "^(?!Live: )"
```

`min` and `max` compare instead of matching, for the fields holding a number —
`duration_seconds` on a video, `itunes_duration` on a podcast. Both bounds are
inclusive, and either side may be written as seconds or with colons, so a length
reads like one:

```json
"duration_seconds_min": "7:00",
"duration_seconds_max": "30:00"
```

Each takes a single bound rather than a list; write both to get a window. A field
that isn't a number — missing, or prose — fails them, so an item whose length
couldn't be established is dropped rather than sent on the assumption it fits.

`published_weekday` is the day an item was published, spelled out (`Monday` …
`Sunday`), which is how a show with a broadcast week gets filtered down to it:

```json
"published_weekday_contains": ["Tuesday", "Wednesday", "Thursday", "Friday"]
```

Like every date rssignal reads, it is **UTC** — an evening upload in the Americas
belongs to the next day here.

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

An item whose enclosure is audio has it downloaded and sent with
`signal-cli --voice-note`. Depending on the file's codec, Signal may show it as
a regular audio attachment rather than an in-app voice note.

### Link previews

Those items also carry a link preview card — the episode title and its artwork —
so an episode is recognizable next to its voice note. Set `"link_preview": false`
to turn it off, or `true` on a feed without audio to turn it on.

**A podcast episode with a card arrives as two messages:** the text and the card
first, then the voice note on its own. Signal silently drops a preview card from
any message that also has an attachment, so they cannot be combined. Setting
`"link_preview": false` goes back to a single message.

The voice note carries the episode title as its body, repeating the card just
above it. That repetition is deliberate: a chat-list row shows a message's own
text and falls back to a bare `🎤 Voice Message` when there is none — and the
voice note is the group's *last* message, so that fallback is what the whole feed
would be labelled with. The title costs one duplicated line in the chat and buys a
readable list. (The `🎤` is drawn by Signal from the attachment itself and cannot
be changed.)

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

Artwork is sent at its original size. Unlike a group avatar, a preview image has
no size limit worth working around — and Signal lays out a large one as a
full-width card rather than a small thumbnail beside the text, which looks
considerably better for podcast covers. Downscaling would only throw that away.

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

### Long messages

Signal caps a message body at 2000 **bytes of UTF-8 — not 2000 characters**. The
difference is punctuation: a curly quote or an em dash costs three bytes, an
accented letter two. An English feed rarely notices. A French or German one can
sail past the limit while still looking comfortably short.

Go over it and Signal silently drops the message's link preview — the whole card,
title and image and all, leaving bare text. Nothing is reported; signal-cli exits
0. (Signal's own apps avoid this by moving the overflow into a `long-message.txt`
attachment instead; signal-cli hands the string over whole.)

So rssignal shortens an over-long body itself, on a word where it can, ending it
with `…`, and counting bytes. **The preview URL is never what gets cut** — Signal
has nothing to draw the card from without it, so the show notes lose their tail
instead. The URL is reserved out of the budget rather than added on top of it,
and it too is measured in bytes, in case of an internationalised domain.

Use a `message_template` if you would rather choose what gets sent than have the
end of it trimmed — `{description}` is the field that tends to be long.
`--dry-run` prints the body as it would actually be sent, already shortened.

## What rssignal remembers

rssignal sends each item once. To do that it has to remember how far each feed
got — and it keeps that **in the feed's own Signal group description**, not in a
state file:

```
Two people argue about films they have not seen. New episode every Thursday.

[rssignal 2026-07-23T10:03:00+00:00]
```

The stamp is the publication date of the newest item that went out. On the next
run, anything published after it is sent, oldest first. Each item's marker is
written just *before* that item's messages, so the group-detail line Signal adds
to the chat sits above the item it belongs to.

There is nowhere better. `signal-cli` cannot read back messages it has sent — the
server hands each message over once, and a linked secondary device never receives
its own sends — and of everything `listGroups` reports, the description is the
only free-text field that is writable, readable back, and stored server-side. The
upside is that the record lives with the group: reinstall signal-cli, or link a
new machine, and nothing is resent.

**The cost:** Signal shows a group-detail change in the chat, so every item sent
also leaves one *"You changed the group description"* line above it in that group.
Runs with nothing new write nothing and stay completely silent.

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
- **A send that fails part-way is safe.** Items go out oldest first, each marked
  just before it is sent. If that send then fails, the previous description is
  put back, so the item is retried on the next run along with everything queued
  behind it. (Should the rollback *also* fail — two Signal errors in a row — that
  one item is lost; rssignal says so on stderr and `--since` replays it.)
- **One bad feed doesn't stop the others.** A feed that fails — a video that
  won't fetch, a source that is down, a group that can't be written to — is
  reported on stderr and skipped, and the run carries on with the feeds after
  it. Its marker stays where it was, so the next run picks it up from the same
  place. `rssignal run` still exits 0 in that case, which is what keeps an
  unattended caller (cron, an Apple Shortcut) from aborting over one feed;
  the failures are on stderr, ending with a `N of M feed(s) failed` line.
  Only something that leaves no feeds to run at all — an unreadable
  `feeds.json` — exits non-zero.
- **A network blip is retried, not lost.** A read that fails for a reason the
  network caused — a name that won't resolve, a refused connection, a reset
  mid-download — is tried again up to three times, two and four seconds apart.
  A laptop that has just woken up is the usual cause, and it is usually gone by
  the second attempt. A feed whose XML simply doesn't parse is not retried:
  it won't parse any better in two seconds.
- **A source blocked on purpose is a skip, not a failure.** See below.

## Blocked sources

If a network-level filter closes a source off from this machine — a NextDNS
profile that only opens YouTube at night, a firewall rule, a scheduled
block — reading its feed is not a failure and rssignal doesn't treat it as
one.

Before a YouTube feed is read, its host is probed with a single HEAD request
(the cheap equivalent of `curl -sI https://www.youtube.com/`, five-second
timeout, no retry). If nothing answers, the feed is skipped with one line:

```
[Last Week Tonight] skipped: https://www.youtube.com/ is not reachable from here
— a DNS or firewall block, not a broken feed. Skipping until it lifts.
1 of 12 feed(s) skipped: source blocked from this machine.
```

Nothing is written to the error log, the skip is counted separately from the
failures, and the feed's marker stays where it was — so the next run inside the
open window sends everything that appeared while it was closed, having lost
nothing.

The probe exists because YouTube is read through yt-dlp, several requests deep,
each willing to wait a minute: without it a blocked run spends minutes per feed
arriving at what one request answers immediately. An HTTP error status counts as
reachable — a 403 or a 404 is the host answering, which is all that's being
asked. Sources read over plain HTTP (ARTE, ordinary RSS) announce an unreachable
host quickly enough on their own and are not probed.

## The error log

stderr is nowhere at all when the run comes from a scheduler or an Apple
Shortcut, so every failure is also appended, with its full traceback, to a log
file:

```
rssignal.log        # in the directory you run from — next to your .env
```

Set `RSSIGNAL_LOG` (in `.env`, or in the environment) to put it somewhere
fixed instead:

```bash
RSSIGNAL_LOG=/Users/you/Library/Logs/rssignal.log
```

`rssignal doctor` prints the path it will use, whether or not the file exists
yet. Each entry is stamped with the local time and says which feed it came
from; the file is rotated to `rssignal.log.1` once it passes 1 MB, so the
failure you came looking for survives the runs after it. A log that can't be
written is a warning on stderr and nothing more — it never stops a message
being sent.

## How fast a run is

A run does as little as it can get away with, in as few waits as it can.

**Feeds are fetched all at once.** Feeds don't depend on each other, and fetching
one is nearly all waiting on somebody else's server, so a run waits once instead
of once per feed. Sending stays strictly sequential — that is where the ordering
promises live (the watermark goes up, then the item goes out, in that order,
per item). `RSSIGNAL_FEED_WORKERS` sets how many at once; the default is 8.

**One `signal-cli` per run, not one per message.** Every `signal-cli` command
starts a JVM, which costs about a second and a half, and a podcast episode used
to spend three of them. A run now keeps a single `signal-cli jsonRpc` process
alive and talks to it over a pipe: the second call costs a round trip instead.
Set `RSSIGNAL_DAEMON=0` to go back to one process per call — slower, but it is
the path rssignal used for years, and the first thing to try if sending ever
misbehaves.

**Answers that can't change are remembered.** An ARTE programme's air date, the
channel a YouTube `@handle` names, and the ETag a feed last handed out are kept
in a cache file, so a run stops re-deriving yesterday's answer:

```
~/.cache/rssignal/cache.json     # or $RSSIGNAL_CACHE
```

Nothing in it decides *what gets sent* — that is the group description, and only
the group description. Deleting the cache costs one slow run and changes nothing
else, which is a property worth keeping: if something ever needs remembering that
*would* change what goes out, it does not belong in there.

Measured on twelve feeds with nothing new to send, which is what a scheduled run
almost always is: **33.8s → 5.1s**.

To see where a run's time actually goes:

```bash
rssignal run --dry-run --timings
```

It prints each step as it finishes and totals them by kind at the end. The
totals can add up to more than the wall clock — that is the parallelism, and
comparing the two is the quickest way to see how much of it there is.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The test suite mocks all `signal-cli` calls, so it never sends real messages —
including the long-lived one, which is disabled outright for tests rather than
left to fall back quietly.

## Release

```bash
./tag_release.sh patch   # or minor / major
```
