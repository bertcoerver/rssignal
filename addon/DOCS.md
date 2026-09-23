# rssignal — Home Assistant add-on

Runs rssignal on a schedule inside Home Assistant OS, so the feeds keep arriving
on a machine that is never asleep.

This exists because the alternative was a laptop. rssignal's macOS setup fires
from an Apple Shortcut three times a day, and about a third of the wrapper
script is there to fight the machine: `caffeinate` to keep it awake, `pmset` to
record when that didn't work, a guard against iCloud evicting the checkout
mid-run. A Raspberry Pi has none of those problems.

## What you need

- Home Assistant OS on aarch64 (Raspberry Pi 4 or 5) or amd64.
- **Booting from a USB SSD is strongly recommended over an SD card.** Every
  podcast and video is written to disk in full before being uploaded, and
  episodes are then kept for two weeks. That is a standing few GB and on the
  order of 100 GB of writes a year, which an SD card will not enjoy.
- A phone with Signal, to link the account.
- A way to get files onto the host and a shell into it: the **Samba share** and
  **Advanced SSH & Web Terminal** add-ons.

## Installing

1. On your own machine, build a wheel into the add-on's build context:

   ```sh
   python3 -m build --wheel --outdir addon/dist .
   ```

   This is how the code gets in. A Docker build can only read files inside the
   folder it is given, so it cannot reach `src/` from `addon/` — the wheel is
   the hand-over. If `addon/dist/` has no wheel in it the build falls back to
   `pip install` from GitHub, which needs the repository to be public **and**
   to have the commit you want on `main`; while this is private, build the
   wheel. Rebuild it whenever you change rssignal itself.

2. Copy the `addon/` directory of this repository to `/addons/rssignal` on the
   Home Assistant host (over Samba, or with `git clone` from the SSH add-on —
   but a clone will not bring the wheel, which is not in git).
3. **Settings → Add-ons → Add-on Store → ⋮ → Check for updates.**
4. rssignal appears under **Local add-ons**. Install it.

Later changes to rssignal are: rebuild the wheel, delete the old one from
`addon/dist/`, copy it over, and **Rebuild** the add-on.

The first build takes several minutes on a Pi 4: it downloads a JRE, the
signal-cli distribution, and a matching native libsignal. It does not do any of
that again unless you change a version.

If the build fails at `install-signal-cli`, read the error — the script is
explicit about which of the two version numbers is wrong. See
[The signal-cli problem](#the-signal-cli-problem).

## Setting up

### 1. feeds.json

Put your `feeds.json` in the add-on's config folder, which the host shows at:

```
/addon_configs/local_rssignal/feeds.json
```

Use `feeds.example.json` from the repository as a starting point. You can edit
it later with the File Editor add-on; the next run picks up the change, no
rebuild needed.

### 2. The account number

Set `account` in the add-on's **Configuration** tab to the E.164 number
rssignal sends from, e.g. `+31600000000`. The add-on refuses to start without
it.

### 3. Link the Signal account

rssignal sends as a linked device, like Signal Desktop, so the container needs
to be linked to your account once. From the **Advanced SSH & Web Terminal**
add-on (with *Protection mode* off):

```sh
docker exec -it addon_local_rssignal bash
export HOME=/data/home
rssignal link
```

Scan the QR code with **Signal → Settings → Linked Devices → Link New Device**.
The command blocks until linking and the initial sync finish.

`HOME` matters: it is where signal-cli keeps the device's keys, and `/data` is
the only directory that survives an add-on update. Linking with the wrong
`HOME` appears to work and then asks to be linked again after the next update.

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
| `schedule` | `01:30`, `06:30`, `15:00` | Local times to run at. |
| `media_keep_days` | `14` | How long sent episodes stay in `/media`. `0` keeps them forever. |
| `update_yt_dlp` | `true` | Upgrade yt-dlp once a day before the first run. |
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

Changing the schedule takes effect when the add-on restarts.

## The media archive

Everything the add-on sends is also kept in Home Assistant's media folder, under
**Media → rssignal**, laid out as:

```
/media/rssignal/<feed>/<YYYY-MM-DD>-<episode-title>.mp3
```

Files are deleted `media_keep_days` after they were archived — counted from when
the file was saved, not when the episode was published, so a back catalogue
released years late still gets its full fortnight.

This costs no extra disk writes. The file is *hard-linked* into place rather
than copied, which is why downloads are staged in `/media/rssignal/.staging`:
the link only works within one filesystem. The `.staging` folder is dot-prefixed
so the Media browser does not show half-finished downloads; a run that is killed
partway leaves a file there, and the next run's expiry pass clears it.

Set `media_keep_days` to `0` to keep everything, and unset it entirely — by
editing `rssignal-run` — to go back to keeping nothing.

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

**To upgrade signal-cli**, edit the two `ARG`s at the top of the `Dockerfile`
together:

1. Set `SIGNAL_CLI_VERSION` to the new version.
2. Read `https://github.com/AsamK/signal-cli/blob/v<version>/libsignal-version`
   and set `LIBSIGNAL_VERSION` to exactly what it says.
3. Check that
   [exquo/signal-libs-build](https://github.com/exquo/signal-libs-build/releases)
   has a `libsignal_v<version>` release. If it does not, wait — do not
   substitute a nearby version. The mismatch is an ABI mismatch.
4. Rebuild the add-on.

The build fails clearly if the two versions disagree.

## Checking on it

`rssignal doctor` is the fastest way to see whether the container is set up
correctly — it checks signal-cli, ffmpeg and yt-dlp on `PATH`, the linked
accounts, the config and the log path:

```sh
docker exec -it addon_local_rssignal bash
export HOME=/data/home
rssignal doctor
```

To see what a run *would* do without sending anything:

```sh
rssignal-run --dry-run
```

To force a run right now, rather than waiting for the next slot:

```sh
rssignal-run
```

`rssignal-run` is the same script the scheduler calls, with the same
environment, so what you see by hand is what happens at 01:30.

Longer-term evidence lives in `/data/rssignal.log`, which records the start and
end of every run and the full traceback of every failure. A "started" line with
no "finished" under it is what a run that was killed partway leaves behind.

## What lives where

| Path (in the container) | On the host | Survives updates |
|---|---|---|
| `/data/home/.local/share/signal-cli` | add-on volume | yes — the device keys |
| `/data/cache.json`, `/data/pending.json` | add-on volume | yes |
| `/data/rssignal.log` | add-on volume | yes |
| `/config/feeds.json` | `/addon_configs/local_rssignal/` | yes |
| `/media/rssignal/` | `/media/rssignal/` | yes |

Of these, only the signal-cli directory and `feeds.json` are irreplaceable. The
cache is a speed cache and deleting it costs one slow run. `pending.json` is
small but not nothing — it is what stops a podcast whose voice note failed from
getting a second copy of its preview card.
