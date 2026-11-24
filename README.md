# rssignal

Send what is new in your feeds to Signal.

rssignal sends each new item of a feed to a Signal group named after the feed:
a podcast episode as a voice note with a preview card, a video as an
attachment, an article as text with its link. It sends by shelling out to the
[`signal-cli`](https://github.com/AsamK/signal-cli) command-line tool, so
messages come from your own linked Signal account.

It downloads nothing itself. [fetchit](https://github.com/bertcoerver/fetchit)
reads the feeds — RSS and podcasts, ARTE, NPO Start, YouTube, Bayam — and files
what is new in a library on disk; rssignal reads that library and sends from it.
Which feeds are read, what is worth downloading and at what quality is fetchit's
config. Which group a feed goes to, what a message looks like and at what pace a
series is released is rssignal's.

## Requirements

- Python 3.13+ (installs `Pillow`)
- A library filled by [fetchit](https://github.com/bertcoerver/fetchit)
- `signal-cli` on your PATH:
  ```bash
  brew install signal-cli        # macOS
  ```
- Optional, for a scannable QR code during linking:
  ```bash
  brew install qrencode
  ```
- Optional, only for files too big for one Signal message (see
  [splitting](#splitting-what-doesnt-fit)):
  ```bash
  brew install ffmpeg
  ```

signal-cli stores its account state in `~/.local/share/signal-cli` by default.

> On a Raspberry Pi, `signal-cli` needs more than installing: the official
> releases carry no native library for aarch64. See
> [the add-on](addon/DOCS.md#the-signal-cli-problem).

## Installation

```bash
pip install -e ".[dev]"
```

To run it on a Raspberry Pi under Home Assistant OS instead of on a laptop, add
this repository to the Add-on Store and install rssignal from there — see
[`addon/DOCS.md`](addon/DOCS.md).

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
   - `RSSIGNAL_LIBRARY` — the folder fetchit downloads into. Optional; defaults
     to `~/fetchit`, which is fetchit's default too.

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

## Running as a service: a GUI, and sending for other things

```bash
RSSIGNAL_SCHEDULE=01:30,06:30,15:00 rssignal serve      # http://127.0.0.1:8099/
```

`rssignal serve` stays running. It keeps one `signal-cli` open for its whole life
(signal-cli allows one process per account, so a second one would wait behind a
run or trip over its lock), runs the feeds at the times in `RSSIGNAL_SCHEDULE`,
and serves on one port:

- **A GUI** at `/`: whether the account is linked, every feed with its group, how
  much of it is waiting and how far it has been sent, run / dry run / picture
  refresh buttons with their output, a form to send a message, recent activity,
  the log, and linking a Signal account by QR code.
- **`POST /api/send`** with `{"to": "Laundry", "message": "..."}` — `to` a group's
  name, a number, `group:<id>` or a list of them; `attachments` a list of base64
  strings or `data:` URIs. Answers `201`, or a JSON `{"error": ...}`.
- **`GET /v1/about`, `POST /v2/send`**: what Home Assistant's built-in
  [Signal Messenger](https://www.home-assistant.io/integrations/signal_messenger/)
  integration calls, so it can use rssignal as its `url` — see
  [the add-on's docs](addon/DOCS.md#notifications-from-home-assistant).

Only this machine may use either, unless told otherwise:

| Variable | Default | |
|---|---|---|
| `RSSIGNAL_SCHEDULE` | (none) | Local times to run at, comma-separated. None: only when asked. |
| `RSSIGNAL_GUI_FROM` | `127.0.0.1,::1` | Addresses or networks that may use the GUI. |
| `RSSIGNAL_API_FROM` | `127.0.0.1,::1` | Addresses or networks that may send. |
| `RSSIGNAL_API_TOKEN` | (none) | A bearer token that lets any address send. |

`--host 0.0.0.0` listens beyond this machine; `--on-start run` runs once at
start. While it is up, use its buttons rather than `rssignal run` in a shell,
which would start a second signal-cli for the same account.

## Feeds

Describe the feeds to send in a JSON config (git-ignored):

```bash
cp feeds.example.json feeds.json
```

Each feed entry supports:

| Key                 | Required | Description                                                            |
| ------------------- | -------- | ---------------------------------------------------------------------- |
| `name`              | yes      | The Signal group this feed sends to. Also `{feed_name}` in templates.  |
| `source`            | no       | The feed in the library to send from — its `name` in fetchit.json. Defaults to `name`. |
| `message_template`  | no       | Message text with `{field}` placeholders. Omit for the built-in layout. |
| `extract`           | no       | Define new fields by regex against existing ones. See below.           |
| `link_preview`      | no       | Send a link preview card. Defaults to on for items with audio, off for the rest. |
| `preview_url`       | no       | Template for the card's link. Defaults to `{link}`.                    |
| `preview_title`     | no       | Template for the card's title. Defaults to `{title}`.                  |
| `preview_description` | no     | Template for the card's text. Omit for a card with no description.     |
| `episodic`          | no       | Send this feed from the beginning, at a set pace. See [episodic feeds](#episodic-feeds). |
| `start`             | no       | Where an episodic feed begins, e.g. `"2019-01-01"`. Episodic feeds only. |
| `refresh_image`     | no       | `false` leaves this feed's group picture alone. See [keeping group pictures current](#keeping-group-pictures-current). |
| `<field>_contains`  | no       | Send only items whose field contains any of these terms.               |
| `<field>_excludes`  | no       | Don't send items whose field contains any of these terms.              |
| `<field>_matches`   | no       | Send only items whose field matches any of these regexes.              |

A feed with the same name in both configs needs nothing but its `name` here:

```json
{"feeds": [{"name": "Le Dessous des Images"}, {"name": "Bureau Buitenland"}]}
```

Unknown keys are rejected, so a typo like `title_contain` is an error rather than a
silently ignored setting. A `url` — or any other key about downloading, like
`since` or `quality` — is rejected with a pointer to fetchit.json, which is where
it belongs now. There is no recency setting either: see
[what rssignal remembers](#what-rssignal-remembers).

Filters here decide what is *sent*, on top of fetchit's, which decide what is
downloaded. Most belong in fetchit — there is no point downloading what will never
be sent — but a filter here is the way to keep everything for Jellyfin and send
only some of it.

### Audio, video and text

What an item is, fetchit has decided already, per item: it files a podcast
episode as audio, an ARTE programme, NPO episode or YouTube video as video, and
anything else as text. rssignal sends each accordingly:

- **Audio** goes out as a voice note, after a [preview card](#link-previews)
  showing the episode's title and its own picture.
- **Video** goes out as the message's attachment: one message, with the title,
  the description and the link as its text.
- **Text** goes out as the title, the description and the link.

A podcast that occasionally posts a written note gets that note as a readable
message with a working link, instead of a linkless stub.

#### Splitting what doesn't fit

This needs **ffmpeg** on your PATH (`brew install ffmpeg`).

Signal refuses an attachment over 100 MB, so a file bigger than 95 MB goes out
in as few parts as will each fit, one message per part. Each part after the
first is captioned with where it falls, `Title (2/3)`. The picture is never
re-encoded: splitting is a stream copy, cut at equal lengths on the nearest
keyframe — seconds of work, not the half hour a Raspberry Pi would spend
re-encoding.

The sound is the one thing that is re-encoded, and only when that alone saves a
message: a video that landed a little over the limit has its soundtrack brought
down to 96 kbps AAC, or to 64 if it takes that, and arrives whole. The file in
the library is never touched; the parts are cut into a temporary folder.

Something too big even for four parts (about 380 MB) is stepped over for good,
watermark and all, so it doesn't block the items behind it.

How many parts a video becomes depends on the quality fetchit downloaded it at.
For a feed sent to Signal, give it `"quality": {"part_size_mb": 95}` in
fetchit.json: the fewest 95 MB parts possible, then the best quality that fits
in that many. Check what a run would send:

```bash
rssignal run --dry-run
# [Le Dessous des Images] -> group:Le Dessous des Images=: La bataille du drapeau
#     video: /media/fetchit/shows/…/Le Dessous des Images - S2026E072301 - La bataille du drapeau.mp4 (~63 MB)
```

### Episodic feeds

Most feeds are tickers: what matters is what arrived since you last looked. A
series is the other thing — its back catalogue *is* the point, and the order to
take it in is the order it was made.

```json
{
  "name": "Last Week Tonight",
  "episodic": "2-weekly",
  "start": "2019-01-01"
}
```

`episodic` changes two things. The feed starts at its **oldest** item instead of
its newest, and it is **paced**: you get one episode every so often rather than
the whole archive at once.

The cadence reads as a rate and is applied as an interval — `"2-weekly"` is two a
week, which is one every three and a half days. That is deliberate: two episodes
dumped every Monday is not what anyone means by "two a week".

| Cadence      | One episode every |
| ------------ | ----------------- |
| `"daily"`    | 24 hours          |
| `"2-daily"`  | 12 hours          |
| `"weekly"`   | 7 days            |
| `"2-weekly"` | 3.5 days          |
| `"monthly"`  | 30 days           |
| `true`       | no pacing at all — the whole catalogue, in one run |

A month is 30 days, so that a rate divides into equal intervals. `true` is the
honest primitive underneath the rest; on a feed with a real archive it will send
the lot, so the string forms are the ones to reach for.

`start` says where the series begins, for a back catalogue you don't want all of.
It acts as the watermark the feed would have had, so everything published before
it counts as already behind you. Without it a series opens at the source's very
first item — which for a channel with nine hundred videos is a long way back.

Worth knowing:

- **There is no catch-up.** A machine that was off for a fortnight releases one
  episode and picks the rhythm back up. It does not owe you four — sending four at
  once is the avalanche the pace was set to avoid, and the queue isn't going
  anywhere.
- **An episode may go out up to four hours early** (at most a quarter of the
  interval). The clock starts when an episode is sent, a few seconds into its run,
  so tomorrow's run at the same time is seconds short of a day. Held strictly, a
  `"daily"` feed whose source is only reachable at one run a day would send every
  other day.
- **A paced feed with nothing due isn't even read**, which is most runs.
- **`--dry-run` always tells you where a series stands** — how many episodes are
  waiting and when the next one is due — rather than going quiet.
- **`--since` overrides the pace**, being an explicit replay. **`--to` touches no
  group**, so it has no clock to read: it sends the next episode and changes
  nothing.

#### Where the back catalogue comes from

An episodic feed can only release what is in the library, and a feed newly
followed by fetchit starts at its newest item. So the back catalogue is fetchit's
to download: give the feed a `since` in fetchit.json, no later than this feed's
`start`, and a `max_per_day` in step with the cadence here:

```json
{"name": "3Blue1Brown", "url": "https://www.youtube.com/@3blue1brown",
 "since": "2019-01-01", "max_per_day": 1}
```

`max_per_day` keeps fetchit a step ahead of the series rather than racing
through the whole archive and having episodes pruned by `keep_days` before they
are sent. For a YouTube channel, fetchit reads the uploads playlist for this,
with no API key; it dates the videos 25 a run, so a long channel takes a day or so
before its oldest episodes are in the library.

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
artwork (`<itunes:image>` for a podcast, the site's logo for a blog, the channel's
avatar for YouTube) as its picture, and the channel's description as its group
description. Both are channel-level — an episode's own image and notes stay on the
message, where they belong. Invite people from Signal on your phone; rssignal never
adds anyone.

The artwork is the feed's in the library — a series' `poster.jpg`, a podcast's
`cover.jpg` — which fetchit fetched from the source; for YouTube, the channel's
avatar.

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
already had is used exactly as it is — rssignal will not change its description or
its expiry. (The one exception is the picture, which it
[keeps current](#keeping-group-pictures-current).) To set them yourself:

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

#### Keeping group pictures current

Podcasts rebrand and channels change their avatar. fetchit keeps the library's
artwork in step with the source, and after every run rssignal puts the library's
artwork on the feed's group — but only where it actually changed.

Changing a group's picture is not silent: everyone in the group sees "changed the
group picture". So rssignal remembers what it last put on each group (by content,
in the cache) and uploads only when the artwork is different. The very first run
has nothing remembered yet, so it sets every group's picture once.

Only groups that already exist are touched. A feed with no artwork in the library
leaves its group's picture alone. Set `"refresh_image": false` to leave a group's
picture alone whatever the library holds — or, better, set it on the feed in
fetchit.json, for a source like ARTE's *Le Dessous des Cartes* whose "artwork" is a
still from the current episode.

To do it right now, or see what it would change:

```bash
rssignal refresh-images --dry-run
rssignal refresh-images
```

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

Which fields exist depends on the feed — most publish more than the standard few,
and fetchit keeps every one of them in the item's record. The `fields` command
samples a real item from the library and lists what you can use:

```bash
rssignal fields                                  # first feed in feeds.json
rssignal fields --feed "Some Podcast"            # by name or source
rssignal fields --source "Some Podcast"          # a feed in the library, not yet in the config
rssignal fields --item 3                         # sample the 3rd newest item
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

Rules read the item as it is in the library, so they can't chain into one another, and an
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

An audio item's file is sent from the library with `signal-cli --voice-note`. Depending on the file's codec, Signal may show it as
a regular audio attachment rather than an in-app voice note.

### Link previews

Those items also carry a link preview card — the episode title and its artwork —
so an episode is recognizable next to its voice note. Set `"link_preview": false`
to turn it off, or `true` on a feed without audio to turn it on.

**A podcast episode with a card arrives as two messages:** the text and the card
first, then the voice note on its own. Signal silently drops a preview card from
any message that also has an attachment, so they cannot be combined. Setting
`"link_preview": false` goes back to a single message.

Two messages means two chances to fail, and the gap between them is the only
moment in a run where an item is neither sent nor unsent. If the card arrives and
the upload doesn't, the item's watermark is rolled back as usual — but the card
that already landed is written down first, and the next run sends only the voice
note:

```
[Pod] 'Episode 214': card already sent, sending only the voice note.
```

That note lives in `~/.cache/rssignal/pending.json` (or `$RSSIGNAL_PENDING`),
written the moment the card lands, because the run that owes the voice note may
not be the run that gets to send it. Deleting it, or losing it to a machine that
can't write it, costs a duplicated card one day and never a lost episode. An item
whose audio is never going to send is written off after a week.

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

The card's artwork is the episode's own picture, which fetchit keeps beside it in
the library (from the feed's `<itunes:image>` or `<media:thumbnail>`). The show's
channel artwork is deliberately *not* a fallback — the same logo on every episode
tells you nothing — so an episode without its own image gets a card without one.

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

## The library

rssignal reads the library and never writes to it. A feed's items are the
`.json` records in its folders under `shows/`, `podcasts/` and `articles/`,
matched by the `name` in each folder's `feed.json`; an item's file and picture are
named in its record. fetchit moves a record into place only once everything it
describes is there, so rssignal never sends half an item, however the two runs
overlap. See [fetchit's README](https://github.com/bertcoerver/fetchit#the-library)
for the layout.

How long things stay in the library is fetchit's `keep_days`. An item pruned before
it was sent is not sent: give a slow feed — an episodic one above all — a
`keep_days` long enough, or a `max_per_day` in step with its cadence.

A feed with nothing in the library yet is skipped with one line, not a failure:

```
[Bureau Buitenland] skipped: the library at /media/fetchit has nothing for 'Bureau Buitenland' yet. …
```

That is a feed fetchit hasn't reached yet — or one whose `source` doesn't match
any feed in fetchit.json, so it says so every run, in the log too.

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

An [episodic feed](#episodic-feeds) keeps a second marker beside the first:

```
Every episode of a show, from the start.

[rssignal 2014-05-04T22:30:00+00:00]
[rssignal-paced 2026-08-08T09:12:44+00:00]
```

That one is a wall-clock time — the moment the last episode was released — and it
is the only thing a pace can be measured from. The two answer different questions,
and neither can be worked out from the other: how far through the series we are,
and when we were last given a piece of it. Both are written in the same update, so
they cannot disagree.

Worth knowing:

- **A feed with no marker yet sends exactly one item** — the newest. A new group
  doesn't open with the whole back catalogue. (An episodic feed opens at the
  *oldest* item instead, and then only one at a time.)
- **Items with no publication date are never sent.** There is no way to tell
  whether they are new.
- **Your own edits survive.** Rewrite a group's description in Signal and rssignal
  moves the markers around your text instead of pasting the feed's blurb back over
  it. Delete the marker and the feed's newest item is sent once more.
- **`--to` touches no group**, so it neither reads nor moves any marker. It sends
  the newest item, every time — or, for an episodic feed, the next episode.
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
- **A network blip is retried, not lost.** Signal resetting the connection partway
  through an upload (`Connection reset (PushNetworkException)`) is common enough
  on a long podcast episode, and it costs the feed its whole run: the marker
  doesn't move, so the next run downloads and uploads the same episode again to
  fail the same way. Those get two more tries, two and four seconds apart. Only
  failures the network caused are retried — a rejected group or a bad recipient
  fails immediately, and so does a local timeout, which would otherwise cost
  another two minutes to arrive at the same answer.
- **Only one run happens at a time.** A run reads a feed's watermark from its
  group and writes it back within that same run, so two runs at once both read
  the same marker, both decide the same items are new, and both send them — a
  duplicate nothing downstream can undo. A run takes an exclusive `flock` and a
  second one stands aside with a line on stderr and exit 0, since the previous
  run being slow is not this one's failure:

  ```
  Nothing to do: another rssignal run is still going (lock held on …/run.lock).
  ```

  This is easy to arrange by accident: a run with videos to fetch takes minutes,
  and a scheduler firing every fifteen doesn't ask whether the last one has
  finished. The lock is held by the kernel, not by a pid file, so it is released
  even when a run is killed outright. `RSSIGNAL_LOCK` moves it; a dry run takes
  no lock at all, and stays usable while a real run is going.
- **An episode that got half-way out is finished, not repeated.** A podcast
  episode with a card is two messages, and a failed upload between them used to
  bring the card back round with the item on the next run — a flaky upload
  retried three times left three identical cards and one episode. The card is
  noted as owed the instant it lands, so the retry sends what is actually
  missing. See [Link previews](#link-previews).
- **A `signal-cli` that dies mid-run costs speed, not the run.** A run keeps one
  `signal-cli jsonRpc` alive for its whole length (see below). If that process
  goes away, the call that noticed redoes itself as a one-shot `signal-cli`, and
  the daemon is retired for the rest of the run rather than rediscovered, feed by
  feed, by everything after it. It is not restarted: one that died once mid-run
  has not earned the rest of it, and the slow path is known to work.
- **A feed with nothing in the library is a skip, not a failure.** See
  [the library](#the-library).

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

Worth doing if your checkout lives in a synced folder — iCloud Drive, Dropbox — 
since a file appended to on every run is what sync makes conflicted copies of.
The cache is already outside the checkout for the same reason.

`rssignal doctor` prints the path it will use, whether or not the file exists
yet. Each entry is stamped with the local time and says which feed it came
from; the file is rotated to `rssignal.log.1` once it passes 1 MB, so the
failure you came looking for survives the runs after it. A log that can't be
written is a warning on stderr and nothing more — it never stops a message
being sent.

### Did the run finish?

Every run also writes one line when it starts and one when it stops:

```
2026-08-06 15:10:02 +0200  run started: 9 feed(s)
2026-08-06 15:12:40 +0200  run finished in 2m38s: 3 item(s) sent
```

This is the only way to tell a run that was *killed* from one that had a quiet
evening — a killed process catches nothing and logs nothing, so without the
start line the two look identical. **A `run started` with no matching line under
it means the run did not get to finish.** If that shows up regularly, whatever
is running rssignal is stopping it early: Apple Shortcuts supervises the shell
scripts it runs and will cut off a long one, and a run with several new videos
in it is easily minutes long. A LaunchAgent has no such limit, and — unlike
plain cron — runs a job it missed once the machine wakes.

The other way a scheduled run ends badly is the machine going back to sleep
under it. That run *does* finish, so it leaves a matching pair of lines — but
macOS stops the monotonic clock while it sleeps, so the duration it reports is
how long it was awake, not how long it took. A run suspended partway says so:

```
2026-08-31 15:00:12 +0200  run started: 13 feed(s)
2026-08-31 18:37:00 +0200  run finished in 5m29s awake, 3h37m wall: 5 item(s) sent — the machine slept partway, so more may have been due. Whatever failed kept its place in the feed, and the next run that stays awake will send it.
```

Worth recognising, because that run's other failures are usually consequences
of the same sleep rather than problems of their own: a download that comes back
`Connection reset by peer`, an upload that dies as `ChatServiceInactiveException`
when signal-cli's connection goes with the machine.

The count is the part that misleads, which is why the line spells it out. A
suspended run most often ends at **zero** items — every send died with the
network — and `0 item(s) sent` on its own is also exactly what a genuinely quiet
evening looks like. So the empty case says which one it was:

```
2026-09-04 08:36:18 +0200  run finished in 1m00s awake, 2h06m wall: 0 item(s) sent — the machine slept partway, so this may be "not delivered" rather than "nothing to deliver". Whatever failed kept its place in the feed, and the next run that stays awake will send it.
```

Nothing is lost either way: a feed that fails rolls its watermark back, so those
items are still queued for the next run rather than skipped over.

A wrapper can hold a power assertion for the length of a run with
`caffeinate -dimsw $$`, but it is not a complete answer: `-s`, the flag that
holds off system sleep, only works on AC power. On battery the run is suspended
regardless.

### Running it somewhere that doesn't sleep

The real answer to all of the above is not to schedule a background job on a
laptop. [`addon/`](addon/DOCS.md) is a Home Assistant add-on that runs rssignal
on a Raspberry Pi: a JRE, ffmpeg and a signal-cli with a working native
library, on a timer, on a machine that is always on — next to the fetchit add-on,
which fills the library it sends from.

Moving is nearly free, because rssignal keeps its place in each feed
[in the group description](#what-rssignal-remembers) rather than on disk. Link
the new machine as a second device and it picks up exactly where the old one
left off — then turn the Shortcut off, because two live hosts on one account
will both send.

The one genuinely awkward part is signal-cli, which ships no native library for
aarch64; the add-on's `install-signal-cli.sh` explains and handles it.

A run stopped in a way rssignal can see (Ctrl-C, a scheduler shutting it down,
an unreadable config) logs `run aborted after …` instead, so that case is not
mistaken for a kill.

### Why has that feed gone quiet?

A feed skipped because the library has nothing for it is not a failure and writes
no traceback, but it does get a line of its own, naming the feed. If that line
keeps coming, check that fetchit has the feed (its own log says whether it was
blocked or failed), and that the names match.

## How fast a run is

A run reads the library — files on the same disk — so almost all of its time is
Signal's.

**One `signal-cli` per run, not one per message.** Every `signal-cli` command
starts a JVM, which costs about a second and a half, and a podcast episode used
to spend three of them. A run keeps a single `signal-cli jsonRpc` process alive
and talks to it over a pipe: the second call costs a round trip instead. Set
`RSSIGNAL_DAEMON=0` to go back to one process per call — slower, but the first
thing to try if sending ever misbehaves.

**A paced feed with nothing due isn't read at all.**

The cache (`~/.cache/rssignal/cache.json`, or `$RSSIGNAL_CACHE`) holds only the
picture last put on each group. Nothing in it decides what gets sent; deleting it
costs at most one redundant picture upload per group. `pending.json` beside it
holds the unsent half of a two-message episode, and losing it costs a duplicated
card, never a lost item.

To see where a run's time actually goes:

```bash
rssignal run --dry-run --timings
```

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
