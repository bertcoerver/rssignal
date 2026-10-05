#!/usr/bin/env bash
#
# Point addon/Dockerfile at a newer signal-cli.
#
# Does the lookups an upgrade needs: finds the newest signal-cli release (or
# takes one as an argument), reads the libsignal version that release pins, and
# checks that exquo has published an aarch64 build of exactly that libsignal.
# Only then does it touch the Dockerfile, and it rewrites both ARGs together,
# because one without the other is an ABI mismatch — see install-signal-cli.sh.
# JAVA_VERSION is raised in the same edit if the release needs a newer Java.
#
#   addon/scripts/bump-signal-cli.sh            # the newest release
#   addon/scripts/bump-signal-cli.sh 0.14.9     # that one, newer or older
#
# Run by hand through `make signal-cli-bump`, and weekly by
# .github/workflows/signal-cli.yml, which turns a change into a pull request.
# It edits the one file and nothing else: no commit, no release.

set -euo pipefail

DOCKERFILE="${DOCKERFILE:-$(dirname "$0")/../Dockerfile}"

pinned() { sed -n -E "s/^ARG $1=\"(.*)\"$/\1/p" "$DOCKERFILE"; }

old_cli="$(pinned SIGNAL_CLI_VERSION)"
old_lib="$(pinned LIBSIGNAL_VERSION)"
old_java="$(pinned JAVA_VERSION)"
if [ -z "$old_cli" ] || [ -z "$old_lib" ] || ! [[ "$old_java" =~ ^[0-9]+$ ]]; then
    echo "!! Could not read the pinned versions from ${DOCKERFILE}." >&2
    exit 1
fi

new_cli="${1:-}"
new_cli="${new_cli#v}"
if [ -z "$new_cli" ]; then
    # Where /releases/latest redirects to, rather than the API: no token, no
    # rate limit, and no JSON to parse.
    latest="$(curl -fsSLI -o /dev/null -w '%{url_effective}' \
        https://github.com/AsamK/signal-cli/releases/latest)"
    new_cli="${latest##*/v}"
fi
if ! [[ "$new_cli" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "!! '${new_cli}' is not a signal-cli version (x.y.z)." >&2
    exit 1
fi

if [ "$new_cli" = "$old_cli" ]; then
    echo "signal-cli ${old_cli} is already the pinned version."
    exit 0
fi

new_lib="$(curl -fsSL \
    "https://raw.githubusercontent.com/AsamK/signal-cli/v${new_cli}/libsignal-version" \
    | tr -d '[:space:]')" || {
    echo "!! signal-cli ${new_cli} has no libsignal-version file: either it is not a" >&2
    echo "!! release, or it is one from before signal-cli started publishing that." >&2
    echo "!!   https://github.com/AsamK/signal-cli/releases" >&2
    exit 1
}

# The same file install-signal-cli.sh will download during the image build.
lib_url="https://github.com/exquo/signal-libs-build/releases/download/libsignal_v${new_lib}/libsignal_jni.so-v${new_lib}-aarch64-unknown-linux-gnu.tar.gz"
if ! curl -fsSLI -o /dev/null "$lib_url"; then
    echo "!! signal-cli ${new_cli} pins libsignal ${new_lib}, and exquo has no aarch64" >&2
    echo "!! build of that yet. Wait for it — a nearby version is an ABI mismatch." >&2
    echo "!!   https://github.com/exquo/signal-libs-build/releases" >&2
    exit 1
fi

# signal-cli publishes no file for the Java it needs the way it does for
# libsignal, so this is read out of its build script. The line has had this
# shape since 0.12; if that changes, JAVA_VERSION is left alone, and the image
# build — which ends by starting signal-cli — is what catches a JRE too old.
# Only ever raised: a newer JRE runs an older signal-cli, so going back a
# release, or a JAVA_VERSION set ahead by hand, is no reason to lower it.
new_java="$(curl -fsSL \
    "https://raw.githubusercontent.com/AsamK/signal-cli/v${new_cli}/build.gradle.kts" \
    | sed -n -E 's/^ *targetCompatibility = JavaVersion\.VERSION_([0-9]+)$/\1/p')" || new_java=""
if ! [[ "$new_java" =~ ^[0-9]+$ ]]; then
    echo "!! Could not tell which Java signal-cli ${new_cli} needs; leaving JAVA_VERSION at ${old_java}." >&2
    new_java="$old_java"
elif [ "$new_java" -lt "$old_java" ]; then
    new_java="$old_java"
fi

# -i with a suffix is the one spelling both GNU and BSD sed accept.
sed -i.bak -E \
    -e "s/^(ARG SIGNAL_CLI_VERSION=)\".*\"$/\1\"${new_cli}\"/" \
    -e "s/^(ARG LIBSIGNAL_VERSION=)\".*\"$/\1\"${new_lib}\"/" \
    -e "s/^(ARG JAVA_VERSION=)\".*\"$/\1\"${new_java}\"/" \
    "$DOCKERFILE"
rm "${DOCKERFILE}.bak"

if [ "$(pinned SIGNAL_CLI_VERSION)" != "$new_cli" ] || [ "$(pinned LIBSIGNAL_VERSION)" != "$new_lib" ] \
    || [ "$(pinned JAVA_VERSION)" != "$new_java" ]; then
    echo "!! ${DOCKERFILE} did not take the new versions." >&2
    exit 1
fi

echo "signal-cli ${old_cli} -> ${new_cli}"
echo "libsignal  ${old_lib} -> ${new_lib}"
if [ "$new_java" != "$old_java" ]; then
    echo "Java       ${old_java} -> ${new_java}"
fi
echo "Release notes: https://github.com/AsamK/signal-cli/releases/tag/v${new_cli}"
