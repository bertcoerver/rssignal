# syntax=docker/dockerfile:1
ARG BUILD_FROM=ghcr.io/hassio-addons/debian-base:9.4.0
FROM ${BUILD_FROM}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Everything version-pinned in one place, because these three move together and
# a mismatch between the first two is an ABI mismatch — see
# scripts/install-signal-cli.sh for why.
#
# To upgrade signal-cli: bump SIGNAL_CLI_VERSION, then read
# https://github.com/AsamK/signal-cli/blob/v<version>/libsignal-version
# and set LIBSIGNAL_VERSION to what it says. The build fails if they disagree.
ARG SIGNAL_CLI_VERSION="0.14.8"
ARG LIBSIGNAL_VERSION="0.102.1"
ARG JAVA_VERSION="25"

# Which rssignal to install, when there is no wheel in `dist/` — see below.
# Not a plain `rssignal[video]` from PyPI because the media archive this add-on
# relies on is not in a release yet. Once it is, this can become
# `rssignal[video]==<version>` and `dist/` can go away.
ARG RSSIGNAL_SPEC="rssignal[video] @ git+https://github.com/bertcoerver/rssignal@main"

# Prepend, never replace. The base image's own s6 services run with this PATH
# too, and one of them ends in `dpkg-reconfigure`, which lives in /usr/sbin —
# so a PATH written out in full that happens to omit /usr/sbin makes that
# service exit 127. The base sets S6_BEHAVIOUR_IF_STAGE2_FAILS=2, meaning any
# failed service takes the whole container down with it, so the symptom is
# rssignal dying at startup because of a timezone script it never touches.
ENV JAVA_HOME=/opt/java \
    PATH="/opt/java/bin:/opt/venv/bin:${PATH}" \
    # Python in a container has no one to buffer for, and an add-on log that
    # only appears once a run is over is no use while the run is going.
    PYTHONUNBUFFERED=1

# ffmpeg is for ARTE (an HLS remux, stream-copy only) and for yt-dlp's muxing.
# zip/unzip are for the libsignal splice. python3-venv because Debian's Python
# is externally managed and rssignal has no business in the system site-packages.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
        git \
        python3 \
        python3-venv \
        # `rssignal link` renders the sgnl:// linking URI as a scannable QR
        # code when this is present, and prints the bare URI when it is not.
        # A bare URI is close to useless: Signal's "Link New Device" only
        # scans, so without this the one interactive step in the whole setup
        # has no way to complete.
        qrencode \
        unzip \
        zip \
    && rm -rf /var/lib/apt/lists/*

# Temurin rather than Debian's openjdk: signal-cli 0.14 requires Java 25, and
# Trixie packages 21.
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
        arm64) java_arch="aarch64" ;; \
        amd64) java_arch="x64" ;; \
        *) echo "unsupported architecture" >&2; exit 1 ;; \
    esac; \
    mkdir -p /opt/java; \
    curl -fsSL -o /tmp/jre.tar.gz \
        "https://api.adoptium.net/v3/binary/latest/${JAVA_VERSION}/ga/linux/${java_arch}/jre/hotspot/normal/eclipse"; \
    tar -xzf /tmp/jre.tar.gz -C /opt/java --strip-components=1; \
    rm /tmp/jre.tar.gz; \
    java -version

COPY scripts/install-signal-cli.sh /usr/local/bin/install-signal-cli
RUN chmod +x /usr/local/bin/install-signal-cli \
    && SIGNAL_CLI_VERSION="${SIGNAL_CLI_VERSION}" \
       LIBSIGNAL_VERSION="${LIBSIGNAL_VERSION}" \
       install-signal-cli

# A wheel built from the working copy, if there is one. The build context is
# only this directory — Docker cannot reach `../src` — so the source has to be
# handed over as a file. `dist/` always exists (it holds a `.keep`) because a
# COPY of a missing path fails the build.
COPY dist/ /tmp/wheels/

# rssignal and yt-dlp in their own venv. Prefer the local wheel: it is the only
# route that works for a repository that is private, or that has commits not
# pushed yet — which is most of the time while this is being worked on. Falling
# back to RSSIGNAL_SPEC keeps a clean checkout buildable on its own.
#
# `git` is needed at this point for the fallback, since setuptools-scm reads the
# tags for the version; it is dropped again below.
RUN set -eux; \
    python3 -m venv /opt/venv; \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip; \
    wheel="$(find /tmp/wheels -maxdepth 1 -name 'rssignal-*.whl' | sort | tail -n 1)"; \
    if [ -n "${wheel}" ]; then \
        echo "installing ${wheel}"; \
        /opt/venv/bin/pip install --no-cache-dir "${wheel}[video]"; \
    else \
        echo "no wheel in dist/, installing ${RSSIGNAL_SPEC}"; \
        /opt/venv/bin/pip install --no-cache-dir "${RSSIGNAL_SPEC}"; \
    fi; \
    rm -rf /tmp/wheels; \
    ln -sf /opt/venv/bin/rssignal /usr/local/bin/rssignal; \
    rssignal --help > /dev/null

# git was only needed for the pip install above; it is a large surface to leave
# in an image whose whole job is to talk to two APIs and a chat server.
RUN apt-get purge -y --auto-remove git \
    && rm -rf /var/lib/apt/lists/*

COPY rootfs /

# s6 runs these; the build should not depend on the file mode surviving a copy
# through git, Samba and a Windows checkout.
RUN chmod +x \
    /etc/s6-overlay/s6-rc.d/rssignal/run \
    /etc/s6-overlay/s6-rc.d/rssignal/finish \
    /usr/local/bin/rssignal-run \
    /usr/local/bin/rssignal-link

LABEL \
    io.hass.name="rssignal" \
    io.hass.description="Forward RSS feeds to Signal" \
    io.hass.type="addon" \
    io.hass.arch="aarch64|amd64"
