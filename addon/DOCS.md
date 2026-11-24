# rssignal — Home Assistant add-on

Runs rssignal on a schedule inside Home Assistant OS, so the feeds keep arriving
on a machine that is never asleep.

rssignal sends; it does not download. The **fetchit** add-on
(`https://github.com/bertcoerver/fetchit`) reads the feeds and files what is new
in `/media/fetchit`, and this add-on sends from there. Install both.

This exists because the alternative was a laptop. rssignal used to fire from an
Apple Shortcut on a Mac, and about a third of the wrapper script was there to
fight the machine: `caffeinate` to keep it awake, `pmset` to record when that
didn't work, a guard against iCloud evicting the checkout mid-run. A Raspberry
Pi has none of those problems.

## What you need

- Home Assistant OS on aarch64 (Raspberry Pi 4 or 5) or amd64.
- The **fetchit** add-on, filling `/media/fetchit`. Its DOCS cover the disk:
  a USB SSD rather than an SD card.
- A phone with Signal, to link the account.
- A way to put `feeds.json` on the host: the **File Editor** or **Samba
  share** add-on. That is the only file you provide; the add-on itself comes
  from this repository. No SSH add-on is needed — nothing here requires a
  shell, because on Home Assistant OS there is no longer a way to get one into
  an add-on.

## Installing

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, and add:

   ```
   https://github.com/bertcoerver/rssignal
   ```

2. rssignal appears in the store under its own heading. Install it.

Nothing is built on the host. GitHub Actions builds the image for each
architecture and publishes it to `ghcr.io`, and installing is a download.

## Updating

When a new version is out, Home Assistant says so — on the add-on's page and
under **Settings → Updates** — with this repository's `CHANGELOG.md` beside it.
Click **Update**. Everything in `/data`, including the linked device, carries
over.

Supervisor looks for new versions on its own every few hours;
**Add-on Store → ⋮ → Check for updates** looks now.

### Publishing a new version

From a checkout with `gh` logged in, once the change is on `main`:

```sh
make addon-release VERSION=0.3.1
```

That starts the **Release add-on** workflow (it can also be started from the
Actions tab). It runs the tests, builds and pushes both images, and only then
bumps `version` in `config.yaml`, adds the commits since the last release to
`CHANGELOG.md`, and tags the commit `addon-v0.3.1`. The order matters:
Supervisor offers an update the moment it sees a new version, so the images
have to exist first. Pull afterwards — the workflow commits to `main`.

If the build fails at `install-signal-cli`, read the error — the script is
explicit about which of the two version numbers is wrong. See
[The signal-cli problem](#the-signal-cli-problem).

### Moving from the old local add-on

Before this repository was public, the add-on was installed by copying `addon/`
to `/addons/rssignal` and building it on the host. Home Assistant treats the
repository version as a *different* add-on — its slug is `a67c9053_rssignal`,
not `local_rssignal` — so it starts with an empty `/data` and its own config
folder. Moving over:

1. Install rssignal from the repository as above. Don't start it yet.
2. Copy `feeds.json` from `/addon_configs/local_rssignal/` to
   `/addon_configs/a67c9053_rssignal/`, and copy the settings on the
   **Configuration** tab across.
3. **Stop** the local add-on, then link the new one — see
   [Link the Signal account](#3-link-the-signal-account). Nothing is resent: the
   watermarks live in the group descriptions, not in `/data`.
4. Start the new one, check the log, then uninstall the local add-on, remove
   its device under **Signal → Settings → Linked Devices**, and delete
   `/addons/rssignal`.

What does not carry over is `/data/cache.json`, which costs one slow run, and
`/data/pending.json`: if a podcast's voice note had failed and was waiting to be
retried, its preview card may be sent a second time.

## Setting up

### 1. feeds.json

Put your `feeds.json` in the add-on's config folder, which the host shows at:

```
/addon_configs/a67c9053_rssignal/feeds.json
```

The prefix is Home Assistant's hash of the repository URL. If you added the
repository under a different spelling — a trailing `/` or `.git` — it will be
another one; it is the folder that ends in `_rssignal`.

Use `feeds.example.json` from the repository as a starting point. A feed's
`name` is its Signal group, and it sends from the feed of the same name in
fetchit's `fetchit.json` unless `source` names another. Which feeds are read and
what is worth downloading is fetchit's config, not this one. You can edit it
later with the File Editor add-on; the next run picks up the change, with no
restart needed.

### 2. The account number

Set `account` in the add-on's **Configuration** tab to the E.164 number
rssignal sends from, e.g. `+31600000000`. The add-on refuses to start without
it.

### 3. Link the Signal account

rssignal sends as a linked device, like Signal Desktop, so the add-on has to be
linked to your account once. The easiest way is the panel: start the add-on,
open **rssignal** in Home Assistant's sidebar, press **Show the QR code**, and
scan it from **Signal → Settings → Linked Devices → Link New Device**. The panel
says *Linked* when the initial sync has finished.

The older way still works, for when the panel is not an option:

1. **Configuration** tab → set `link_mode` to **true** → Save.
2. **Start** the add-on.
3. Open Home Assistant's **Media** panel. There is now a file called
   **`rssignal-link.png`** — a QR code.
4. Scan it with **Signal → Settings → Linked Devices → Link New Device**.
5. Wait. The add-on log says `Linked.` when the initial sync has finished, and
   the QR code is deleted at that moment.
6. Set `link_mode` back to **false** → Save → **Start** the add-on again.

The add-on log carries a text copy of the same QR code, which is quicker if it
happens to render legibly in your browser. The PNG is the one to rely on.

> **The QR code is a credential.** Anyone who scans it before you do gets a
> linked device on your Signal account, and while it exists it sits in a folder
> every Home Assistant user can browse. The add-on deletes it as soon as linking
> finishes, or when it is stopped. Don't leave `link_mode` on.

**Why not a shell?** Because there isn't one. Linking used to be
`docker exec -it addon_local_rssignal rssignal link`, which needs the add-on to
run with protection mode off — and Home Assistant 2026.6 removed the Advanced
Mode setting that exposed that toggle. There is no longer a supported way to get
a shell into an add-on, so linking had to become something the add-on does to
itself.

> **Do not copy signal-cli's state from your Mac.** Link a second device
> instead. It is less fiddly and Signal is built for it. rssignal keeps no local
> record of what it has sent — the watermark for each feed lives in that feed's
> Signal *group description*, on the server — so a freshly linked device picks
> up exactly where the old one left off.

### 4. Start it, then retire the old one

Start the add-on and watch the log. Once you have seen a real run arrive in
Signal, **turn off the Apple Shortcut automation on the Mac.** Two hosts sending
from one account will both send, and will race each other writing the group
descriptions that hold the watermarks.

## Configuration

| Option | Default | What it does |
|---|---|---|
| `account` | — | E.164 number to send from. Required. |
| `link_mode` | `false` | Link to Signal instead of running. See above. |
| `on_start` | `wait` | `wait`, `dry-run` or `run` — what to do at startup, before the schedule takes over. |
| `schedule` | `01:30`, `06:30`, `15:00` | Local times to run at. |
| `library` | `/media/fetchit` | The library the fetchit add-on fills. |
| `api_allow` | `172.30.32.1` | Addresses that may send through rssignal: Home Assistant itself. |
| `api_token` | (none) | A bearer token that lets anything else on Home Assistant's network send. |
| `log_level` | `info` | Add-on log verbosity. |

### About the schedule

Keep three or so runs spread across the day rather than one. rssignal paces
episodic feeds by comparing the time since the last release against the feed's
cadence, with a four-hour tolerance (`EARLY` in `rssignal/episodic.py`) sized
for exactly this: without it, a run at 06:30 today is a few seconds short of 24
hours after the run at 06:30 yesterday, and a `daily` feed quietly becomes an
every-other-day one.

Running more often is harmless — nothing is ever sent twice, and a run with
nothing to do takes a few seconds — but there is little to gain.

Put the times a little after fetchit's (its defaults are 01:00, 06:00 and
14:30), so each run finds what the last download brought. Nothing breaks if
they overlap: fetchit moves an item into place only once all of it is there.

Changing the schedule takes effect when the add-on restarts.

## The panel

Once started, the add-on is **rssignal** in Home Assistant's sidebar (or **Open
Web UI** on its page). It shows whether the account is linked and when the next
run is; every feed with its group, how many of its items are waiting, how far it
has been sent, and when an episodic feed's next episode is due; what was sent
recently, by a run, by Home Assistant or by hand; and the log. Buttons start a
run, a dry run or a group picture refresh now, with what it printed shown as it
goes; a form sends a message to any group or number.

It is reached through Home Assistant's ingress, so it is behind your Home
Assistant login and nothing else can open it.

## Notifications from Home Assistant

rssignal can send messages for Home Assistant — "the washing machine is done",
"someone is at the door" — through the same linked account. There are two ways
in. Both are reached at `http://<hostname>:8099`, where `<hostname>` is shown on
the add-on's **Info** page (something like `a67c9053-rssignal`); the panel shows
the snippet below already filled in.

### Home Assistant's Signal Messenger integration

rssignal speaks the part of `signal-cli-rest-api` that Home Assistant's built-in
[Signal Messenger](https://www.home-assistant.io/integrations/signal_messenger/)
integration uses, so it works as it is. In `configuration.yaml`:

```yaml
notify:
  - platform: signal_messenger
    name: signal
    url: "http://a67c9053-rssignal:8099"
    number: "+31600000000"        # the account in this add-on's configuration
    recipients:
      - "Laundry"                 # a group's name, a number, or group:<id>
```

Restart Home Assistant, and `notify.signal` sends to the Laundry group:

```yaml
automation:
  - alias: Washing machine is done
    triggers:
      - trigger: state
        entity_id: sensor.washing_machine_status
        to: "done"
    actions:
      - action: notify.signal
        data:
          message: "The washing machine is done."
```

`target:` in the action sends one message somewhere else instead
(`target: ["+31611111111"]`), and `data: {urls: [...]}` or `data: {attachments:
[...]}` attaches a picture — a camera snapshot, say.

Recipients are matched to groups by name, ignoring case. rssignal does not
create a group for a notification: make it in Signal first (an announcement-only
group with just you in it is what a feed gets too), and invite whoever should
hear about the washing.

### Anything else: `/api/send`

```yaml
rest_command:
  signal:
    url: "http://a67c9053-rssignal:8099/api/send"
    method: POST
    content_type: "application/json"
    payload: '{"to": "{{ to }}", "message": "{{ message }}"}'
```

`to` is a group's name, a number, `group:<id>`, or a list of them;
`attachments` is a list of base64 strings or `data:` URIs.

### Who may send

Home Assistant reaches the add-on over its internal network; no port is
published on yours. Requests are accepted from the addresses in `api_allow`
(Home Assistant is `172.30.32.1`) and from anything presenting `api_token` as
`Authorization: Bearer <token>`. A refused request is named in the log and in
the panel's activity, with its address, so you can see what to allow.

## Watching what was sent

The library in `/media/fetchit` is laid out for Jellyfin: videos as series with
seasons, podcasts as albums. See the fetchit add-on's DOCS for setting up a
Jellyfin library on it, and Infuse on top of that. How long things stay there
is fetchit's `keep_days`.

## The signal-cli problem

Worth understanding before you upgrade anything.

signal-cli is a Java program with a Rust core (`libsignal`) shipped as a native
library inside a jar. The official releases build that library for **x86-64
Linux, Windows and macOS only**. On aarch64 — every Raspberry Pi — the jar
contains nothing the JVM can load, and signal-cli dies on its first real call.

`scripts/install-signal-cli.sh` fixes this at image build time: it downloads a
third-party aarch64 build of the *same* libsignal version from
[exquo/signal-libs-build](https://github.com/exquo/signal-libs-build) and puts
it into the jar under the name the loader looks for
(`libsignal_jni_aarch64.so`). It then checks that what landed there really is an
ELF object for really this architecture, so a bad download fails the build
rather than the first send.

It also needs a **Java 25** runtime, which signal-cli has required since 0.14.0
and Debian Trixie does not package — so the image pulls a Temurin JRE.

**To upgrade signal-cli**, the `ARG`s at the top of the `Dockerfile` have to
change together, and `make signal-cli-bump` does that:

1. It takes the newest signal-cli release, or `SIGNAL_CLI=x.y.z` if you name
   one, and sets `SIGNAL_CLI_VERSION` to it.
2. It reads `https://github.com/AsamK/signal-cli/blob/v<version>/libsignal-version`
   and sets `LIBSIGNAL_VERSION` to exactly what it says.
3. It checks that
   [exquo/signal-libs-build](https://github.com/exquo/signal-libs-build/releases)
   has a `libsignal_v<version>` release, and changes nothing if it does not.
   Then wait — do not substitute a nearby version. The mismatch is an ABI
   mismatch.
4. It reads the Java version the release is built for out of signal-cli's
   `build.gradle.kts`, and raises `JAVA_VERSION` if that is newer.

Commit and push the result, and [publish a new version](#publishing-a-new-version).

You should rarely need to run it yourself. The **Check signal-cli** workflow
(`.github/workflows/signal-cli.yml`) runs the same script every Monday, builds
the aarch64 image with the new versions, and opens a pull request if that
works. A patch release (0.14.8 → 0.14.9) is merged there and then, and only
publishing a new version is left; anything bigger waits for you to read the
release notes and merge it. A run that fails means a release is out that
cannot be used yet, usually because exquo has not built its libsignal. The
pull request needs *Settings → Actions → General →
Allow GitHub Actions to create and approve pull requests* switched on.

The Java version is the one lookup with no file published for it, so it is the
one that could stop working if signal-cli rearranges its build script. The
script then says so and leaves `JAVA_VERSION` alone; the image build ends by
running `signal-cli --version`, so a JRE that is too old fails the build rather
than publishing an image that cannot start.

The build also fails clearly if the two versions disagree, and a failed build
publishes nothing.

## Checking on it

There is no shell into an add-on on Home Assistant OS (see
[Link the Signal account](#3-link-the-signal-account)), so everything you would
normally type is reported or triggered from the Configuration tab instead.

**`rssignal doctor` runs on every start** and its output goes to the add-on log.
It checks signal-cli and ffmpeg on `PATH`, which accounts are linked, the
library, the config and the log path — so restarting the add-on is the way to ask "is
this thing healthy?". The answer is the block of indented lines near the top of
the log, under `Checking the install...`.

**To make a run happen now**, use the panel's buttons. Or set `on_start` and
restart:

| `on_start` | What happens at startup |
|---|---|
| `wait` | Nothing; sleeps until the next scheduled slot. The normal setting. |
| `dry-run` | Reads every feed, reports what it would send, sends nothing. |
| `run` | A real run, immediately. |

Set it back to `wait` afterwards, or every restart will fire a run.

`dry-run` is the one to use after editing `feeds.json`: it reads every feed
from the library — and deliberately lifts the pacing gate that normally holds
episodic feeds back — without sending anything.

Longer-term evidence lives in `/data/rssignal.log`, which records the start and
end of every run and the full traceback of every failure. A "started" line with
no "finished" under it is what a run that was killed partway leaves behind. It
is inside the add-on's volume, so the way to read it is the add-on log, or a
backup.

## What lives where

| Path (in the container) | On the host | Survives updates |
|---|---|---|
| `/data/signal-cli` | add-on volume | yes — the device keys |
| `/data/cache.json`, `/data/pending.json` | add-on volume | yes |
| `/data/rssignal.log` | add-on volume | yes |
| `/config/feeds.json` | `/addon_configs/a67c9053_rssignal/` | yes |
| `/media/fetchit/` | `/media/fetchit/` | fetchit's library, read only |

Of these, only the signal-cli directory and `feeds.json` are irreplaceable. The
cache is a speed cache and deleting it costs one slow run. `pending.json` is
small but not nothing — it is what stops a podcast whose voice note failed from
getting a second copy of its preview card.
