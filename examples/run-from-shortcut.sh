#!/bin/sh
# The script the "Run Shell Script" action of an Apple Shortcut runs, three
# times a day, to fire rssignal unattended. Kept here because what a scheduled
# run does before it reaches `rssignal run` is half of what the error log ends
# up saying, and none of it is visible from inside rssignal itself.
#
# Shortcuts starts the script with a bare PATH and no working directory, so both
# are set first: `cd` is what makes rssignal find feeds.json and .env, and the
# Homebrew prefix is what makes it find signal-cli, ffmpeg and yt-dlp.

export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin"

# The repo lives in iCloud Drive, which is allowed to evict a directory that has
# not been touched in a while. A failed `cd` would otherwise leave rssignal
# running somewhere else entirely, where there is no feeds.json to read and no
# .env to load — so it is a hard stop, not a warning.
REPO="/Users/hmcoerver/Library/Mobile Documents/com~apple~CloudDocs/Repositories/rssignal"
cd "$REPO" || exit 1

# A sidecar to rssignal's own log, for the handful of facts that are true of the
# run before rssignal starts and can't be recovered afterwards. Kept separate so
# nothing here races rssignal's log rotation.
SHORTCUT_LOG="$HOME/Library/Logs/rssignal-shortcut.log"

# The scheduler fires this inside a macOS DarkWake window capped at ~3 minutes.
# A run with a video in it is routinely longer than that, and a Mac that dozes
# off mid-run takes signal-cli's connection to Signal with it — the upload then
# dies as ChatServiceInactiveException. Hold a power assertion for as long as
# this script lives; it releases itself when the script exits.
#
# -s is the one that matters and the one with a catch: it suppresses system
# sleep *only on AC power*. On battery it is a no-op, -i holds off idle sleep
# but not the end of the maintenance window, and the run gets suspended
# regardless — which is what happened every run between 27 and 31 August 2026.
# Nothing here can fix that, so the power state is recorded instead: it is the
# first thing worth knowing when a run's wall time and its reported duration
# disagree, and there is nowhere else it survives.
/usr/bin/caffeinate -dimsw $$ &
printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')" "$(pmset -g batt | tail -1)" \
    >>"$SHORTCUT_LOG" 2>/dev/null

# rssignal goes first. The DarkWake budget is spent on the thing the run is for,
# rather than on a `brew update` that can just as well happen once the messages
# are out — a stale yt-dlp is at most one run behind either way.
/Users/hmcoerver/.local/bin/rssignal run
status=$?

# yt-dlp goes stale fast: YouTube changes something, and every video feed fails
# until it is updated. Homebrew's copy is the one the PATH above resolves, so
# Homebrew is what has to do the upgrading — `yt-dlp -U` refuses to touch a
# package-manager install. Never fatal: a bad update morning should still cost
# the podcasts nothing, and it must not turn a clean run into a failed one.
brew update --quiet && brew upgrade --quiet yt-dlp || true

exit $status
