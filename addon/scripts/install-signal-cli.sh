#!/usr/bin/env bash
#
# Install signal-cli, with a native libsignal that matches this machine.
#
# The whole reason this is a script and not two lines of Dockerfile: the
# official signal-cli release ships native libsignal for x86-64 Linux, Windows
# and macOS only. On aarch64 — every Raspberry Pi — the jar contains no library
# the JVM can load, and signal-cli dies on its first call with an
# UnsatisfiedLinkError. The fix is to put an aarch64 build of the same libsignal
# version into the jar, and the builds come from a third party, because there is
# no first-party one.
#
# libsignal names its bundled library after `os.arch`: the stock jar holds
# `libsignal_jni_amd64.so` and `libsignal_jni_aarch64.dylib`, so what an aarch64
# Linux JVM goes looking for is `libsignal_jni_aarch64.so`. That is the name
# this script adds.
#
# Two things make the swap safe to automate rather than alarming:
#
#   * The libsignal version is not chosen here. It is whatever the signal-cli
#     release pinned — signal-cli publishes it in a `libsignal-version` file,
#     and the jar in the tarball is named after it. A mismatched library is an
#     ABI mismatch, i.e. a crash, so LIBSIGNAL_VERSION below must be kept in
#     step with SIGNAL_CLI_VERSION whenever either is bumped.
#   * Nothing is taken on trust. The script ends by checking that what ended up
#     in the jar is really an ELF object for really this architecture, so a
#     failed download or a wrong build fails the *image build*, loudly, rather
#     than at 01:30 some morning with nobody watching.
#
# On amd64 the stock jar is already correct and the splice is skipped entirely.
#
# Sources, if exquo ever lacks a version:
#   https://github.com/AsamK/signal-cli/wiki/Provide-native-lib-for-libsignal
#   https://media.projektzentrisch.de/temp/signal-cli/

set -euo pipefail

SIGNAL_CLI_VERSION="${SIGNAL_CLI_VERSION:?}"
LIBSIGNAL_VERSION="${LIBSIGNAL_VERSION:?}"
PREFIX="${PREFIX:-/opt/signal-cli}"

SIGNAL_CLI_URL="https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}.tar.gz"
LIBSIGNAL_BASE="https://github.com/exquo/signal-libs-build/releases/download/libsignal_v${LIBSIGNAL_VERSION}"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "==> signal-cli ${SIGNAL_CLI_VERSION} (libsignal ${LIBSIGNAL_VERSION})"

# --- the distribution -------------------------------------------------------

curl -fsSL -o "${work}/signal-cli.tar.gz" "$SIGNAL_CLI_URL"
mkdir -p "$PREFIX"
tar -xzf "${work}/signal-cli.tar.gz" -C "$PREFIX" --strip-components=1

jar="${PREFIX}/lib/libsignal-client-${LIBSIGNAL_VERSION}.jar"
if [ ! -f "$jar" ]; then
    echo "!! Expected ${jar}, which is not there." >&2
    echo "!! signal-cli ${SIGNAL_CLI_VERSION} pins a different libsignal than" >&2
    echo "!! LIBSIGNAL_VERSION says. Check:" >&2
    echo "!!   https://github.com/AsamK/signal-cli/blob/v${SIGNAL_CLI_VERSION}/libsignal-version" >&2
    ls -1 "${PREFIX}/lib/" | grep -i libsignal >&2 || true
    exit 1
fi

# --- the native library -----------------------------------------------------

# Debian's name for the architecture, translated to the one the JVM reports and
# the one the libsignal builds are labelled with. They agree on aarch64 and
# disagree on everything else, which is exactly the sort of thing to get wrong.
case "$(dpkg --print-architecture)" in
    arm64)  jvm_arch="aarch64"; rust_target="aarch64-unknown-linux-gnu" ;;
    amd64)  jvm_arch="amd64";   rust_target="" ;;
    *)
        echo "!! Unsupported architecture $(dpkg --print-architecture)" >&2
        exit 1
        ;;
esac

if [ -n "$rust_target" ]; then
    echo "==> Splicing in a ${rust_target} libsignal"
    curl -fsSL -o "${work}/lib.tar.gz" \
        "${LIBSIGNAL_BASE}/libsignal_jni.so-v${LIBSIGNAL_VERSION}-${rust_target}.tar.gz"
    tar -xzf "${work}/lib.tar.gz" -C "$work"

    # The jar wants the library at its root, under the arch-suffixed name.
    mv "${work}/libsignal_jni.so" "${work}/libsignal_jni_${jvm_arch}.so"
    ( cd "$work" && zip -q "$jar" "libsignal_jni_${jvm_arch}.so" )
fi

# The bundled x86-64 library is ~194 MB of code this machine can never run, and
# it would otherwise sit in every layer of the image for the life of the
# install. Dropping it is not an optimisation so much as basic hygiene.
for dead in libsignal_jni_amd64.so libsignal_jni_amd64.dylib \
            libsignal_jni_aarch64.dylib signal_jni_amd64.dll; do
    if [ "$dead" != "libsignal_jni_${jvm_arch}.so" ]; then
        zip -q -d "$jar" "$dead" >/dev/null 2>&1 || true
    fi
done

# --- prove it works ---------------------------------------------------------

ln -sf "${PREFIX}/bin/signal-cli" /usr/local/bin/signal-cli

echo "==> Verifying"

# Check the library's ELF header rather than trying to make signal-cli load it.
# The tempting check is `signal-cli --version`, but that prints and exits before
# any Signal protocol work happens, so it would pass with no usable library at
# all — the failure would surface on the first real send instead, which is the
# one place it must not. What can go wrong here is a download that silently
# returned an error page, or a library for the wrong architecture, and the ELF
# header answers both directly.
#
# e_machine lives at offset 18 in the header: 0xB7 is AArch64, 0x3E x86-64.
case "$jvm_arch" in
    aarch64) want="b7 00" ;;
    amd64)   want="3e 00" ;;
esac

unzip -p "$jar" "libsignal_jni_${jvm_arch}.so" > "${work}/check.so"

head -c 4 "${work}/check.so" | grep -q 'ELF' || {
    echo "!! libsignal_jni_${jvm_arch}.so is not an ELF object at all —" >&2
    echo "!! the download probably returned an error page, not a library." >&2
    exit 1
}

got="$(od -An -tx1 -j18 -N2 "${work}/check.so" | tr -s ' ' | sed 's/^ //;s/ $//')"
if [ "$got" != "$want" ]; then
    echo "!! libsignal_jni_${jvm_arch}.so is not a ${jvm_arch} object" >&2
    echo "!! (ELF e_machine was '${got}', expected '${want}')" >&2
    exit 1
fi

# And that the distribution itself is intact and the JRE can run it. This does
# not exercise libsignal, for the reason above; the line before it does.
signal-cli --version

echo "==> signal-cli ${SIGNAL_CLI_VERSION} installed with a ${jvm_arch} libsignal"
