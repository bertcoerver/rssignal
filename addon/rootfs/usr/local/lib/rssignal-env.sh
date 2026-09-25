# shellcheck shell=bash
#
# The environment every rssignal command in this add-on runs with.
#
# Sourced rather than duplicated, because the alternative is two copies that
# drift: the scheduler runs `rssignal doctor` to report where things are, and if
# it reported different paths than the ones `rssignal run` actually uses, the
# one diagnostic available on a host with no shell would be lying.
#
# Expects bashio to be loaded — every caller is a `with-contenv bashio` script.

# /data is the add-on's persistent volume: it survives restarts, updates and
# rebuilds. Everything that must not be lost goes there, and nothing else does.
#
# HOME is the important one and the least obvious. signal-cli keeps the linked
# device's identity — its keys — under $HOME/.local/share/signal-cli, and
# rssignal invokes signal-cli without a --config flag, so this variable is the
# only lever there is. Get it wrong and the add-on asks to be linked again
# after every update.
export HOME="/data/home"
mkdir -p "${HOME}"

# ...except that signal-cli does not read HOME, and this is the single most
# expensive thing to get wrong in the whole add-on.
#
# signal-cli resolves its data directory in IOUtils.getDataHomeDir(): it takes
# $XDG_DATA_HOME if that is set, and otherwise falls back to the JVM property
# `user.home`. That property is not the HOME environment variable. On Linux the
# JVM fills it in from the passwd database via getpwuid(), so in a container
# running as root it is `/root` no matter what HOME says. signal-cli therefore
# wrote the linked device's keys to /root/.local/share/signal-cli — and
# Supervisor builds a fresh container from the image on every start, so /root is
# gone by the time anything reads it.
#
# The symptom is peculiarly convincing: linking reports success, Signal shows
# the new device on the phone, and the very next start says "linked accounts:
# none". It looks like the link failed. It did not; the keys were simply written
# somewhere with the lifetime of one container.
#
# XDG_DATA_HOME is checked before `user.home` and is read as an ordinary
# environment variable, so it is the one lever that works.
export XDG_DATA_HOME="/data"
mkdir -p "${XDG_DATA_HOME}/signal-cli"

# rssignal's own files. The cache and pending list default to ~/.cache, which
# HOME above would already have put on /data — said outright anyway, because
# one of them decides whether a half-sent episode gets its second message.
export RSSIGNAL_CACHE="/data/cache.json"
export RSSIGNAL_PENDING="/data/pending.json"
export RSSIGNAL_LOCK="/data/run.lock"
export RSSIGNAL_LOG="/data/rssignal.log"

RSSIGNAL_ACCOUNT="$(bashio::config 'account')"
export RSSIGNAL_ACCOUNT

# /media is the folder Home Assistant's Media browser shows. rssignal links
# each episode it sends into here and expires them on the schedule below; with
# RSSIGNAL_MEDIA_DIR unset it would delete every download as it always has.
export RSSIGNAL_MEDIA_DIR="/media/rssignal"
RSSIGNAL_MEDIA_KEEP_DAYS="$(bashio::config 'media_keep_days')"
export RSSIGNAL_MEDIA_KEEP_DAYS

# A run refreshes every feed group's picture on one run in N, at random (see
# rssignal.artwork). N is only "about once a month" for a given number of runs
# a day, and the schedule says how many that is — so it is worked out here
# rather than left at the default, which assumes fifteen.
runs_per_day="$(jq -r '.schedule[]? // empty' /data/options.json | grep -c . || true)"
export RSSIGNAL_ARTWORK_REFRESH_ONE_IN="$(( ${runs_per_day:-0} * 30 ))"

# feeds.json lives on the host at /addon_configs/<slug>/, so it can be edited
# with the File Editor or over Samba without rebuilding anything.
export RSSIGNAL_CONFIG="/config/feeds.json"
