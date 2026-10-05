"""Send what doesn't fit in one attachment as several: as few as possible.

Signal refuses an attachment over 100 MB, and that used to be the end of the
matter — a video no quality of which fitted was skipped for good, and a long
podcast episode failed at upload every run. Splitting the file and sending the
pieces as consecutive messages keeps the episode, at the cost of a seam or two.

The rule, for every source and for audio as well as video:

    **the fewest parts possible, then the best quality that fits in that many.**

So a video whose smallest rung is 105 MB is two parts, and within those two
parts' 190 MB it goes up to the sharpest rung that still fits — not three parts
of 1080p, and not two parts of the rung that only needed one. Quality is chosen
before downloading by :func:`choose`, from each source's own size estimates;
how many parts it actually becomes is decided after, by :func:`split`, from the
size of the file that landed. An estimate that ran low therefore costs one more
part, never the episode.

Splitting is a stream copy, cut at equal lengths with ffmpeg's segment muxer.
The picture is never re-encoded — on a Raspberry Pi that is the difference
between seconds and half an hour — so the cuts snap to the nearest keyframe and
the parts come out close to, not exactly, equal. A part that lands over the
limit is simply cut again one part finer.

The sound is another matter: it encodes at many times real time on one core,
and in a lean video it can be a third of the file — NPO's 360p carries 256 kbps
of it beside 400 of picture. So before a video is cut, :func:`slim` works out
whether a leaner soundtrack alone would save a message, and re-encodes just
that if so: a 25-minute episode that landed at 120 MB then arrives whole, at
about 91, with its picture untouched.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, TypeVar

from . import timing
from .errorlog import log_line
from .feeds import FeedError
from .video import VIDEO_MAX_BYTES, VideoTooBig

T = TypeVar("T")

# Signal's limit is per attachment, whatever is in it: audio gets the same
# budget as video.
MAX_BYTES = VIDEO_MAX_BYTES

# Past this many parts an item is still too big, and skipped for good. Four
# parts is ~380 MB: a two-hour documentary at 360p, or a three-hour podcast at
# a generous bitrate. Anything bigger is a different kind of thing to be
# sending to a chat.
MAX_PARTS = 4

# A stream copy of a few hundred MB from local disk. Minutes would mean
# something is wrong.
SPLIT_TIMEOUT = 300

# What a video's sound is re-encoded to when that alone saves a message, best
# first: the first rate that brings the part count down is the one used. Both
# are AAC, like every source's own soundtrack, so nothing downstream changes.
SLIM_AUDIO_BITRATES = (96_000, 64_000)

# The slimmed size is worked out before any work is done, and the encoder lands
# within a fraction of a percent of it. A prediction closer to the limit than
# this is not worth the minutes it would take to find out.
SLIM_MARGIN = 1.02

# An audio encode of a whole video, on one core of a Raspberry Pi.
SLIM_TIMEOUT = 900

# Containers that want their index at the front to start playing before the
# whole part has arrived. The same reason ARTE's download asks for it.
_FASTSTART_EXTS = (".mp4", ".m4a", ".m4v", ".mov")


def parts_needed(size: int, max_bytes: int = MAX_BYTES) -> int:
    """How many parts of at most ``max_bytes`` a ``size``-byte file needs."""
    return max(1, math.ceil(size / max_bytes))


def choose(
    candidates: Sequence[T],
    *,
    size: Callable[[T], int],
    rank: Callable[[T], Any],
    max_bytes: int = MAX_BYTES,
    max_parts: int = MAX_PARTS,
) -> tuple[T, int] | None:
    """Pick ``(candidate, parts)``: the fewest parts, then the best in them.

    ``size`` estimates a candidate's bytes and ``rank`` orders them best-last.
    ``None`` means even the smallest needs more than ``max_parts``; the caller
    says why in its own words, since it knows what the candidates were.
    """
    if not candidates:
        return None
    parts = parts_needed(min(size(c) for c in candidates), max_bytes)
    if parts > max_parts:
        return None
    budget = parts * max_bytes
    fitting = [c for c in candidates if size(c) <= budget]
    return max(fitting, key=rank), parts


def in_parts(parts: int) -> str:
    """The suffix a dry run adds to a plan's description: ``", in 2 parts"``."""
    return f", in {parts} parts" if parts > 1 else ""


def too_big(what: str, smallest: int, *, max_bytes: int = MAX_BYTES) -> VideoTooBig:
    """The :class:`VideoTooBig` for ``what`` whose smallest option is ``smallest``."""
    mb = 1024 * 1024
    return VideoTooBig(
        f"{what} is too big to send: {smallest / mb:.0f} MB is over "
        f"{MAX_PARTS} parts of {max_bytes / mb:.0f} MB"
    )


@contextmanager
def split_temp(
    path: str,
    *,
    max_bytes: int = MAX_BYTES,
    max_parts: int = MAX_PARTS,
    timeout: float = SPLIT_TIMEOUT,
) -> Iterator[list[str]]:
    """Yield ``path`` as a list of parts that each fit, deleting any made after.

    A file that already fits is yielded as itself, ``[path]``, and costs
    nothing. One that doesn't is first given the chance to fit in fewer parts
    by :func:`slim`; what is left to cut is cut. Everything made here lives in
    its own temporary directory, which goes away when the ``with`` block ends —
    ``path`` itself is the caller's and is left alone.

    Raises :class:`~rssignal.video.VideoTooBig` if even ``max_parts`` parts
    would each be over the limit, and :class:`~rssignal.feeds.FeedError` if
    ffmpeg is missing or fails.
    """
    if os.path.getsize(path) <= max_bytes:
        yield [path]
        return
    with tempfile.TemporaryDirectory(prefix="rssignal-parts-") as into:
        # In a directory of its own so it keeps the file's name, and the parts
        # cut from it are named as they would have been without it.
        aside = os.path.join(into, "slim")
        os.mkdir(aside)
        path = slim(path, aside, max_bytes=max_bytes) or path
        if os.path.getsize(path) <= max_bytes:
            yield [path]
            return
        yield split(
            path, into, max_bytes=max_bytes, max_parts=max_parts, timeout=timeout
        )


def slim(
    path: str,
    into: str,
    *,
    max_bytes: int = MAX_BYTES,
    timeout: float = SLIM_TIMEOUT,
) -> str | None:
    """Re-encode a video's sound leaner if that saves a part; the new file's path.

    ``None`` — and no work done — unless ``path`` is a video whose soundtrack is
    heavy enough that one of :data:`SLIM_AUDIO_BITRATES` would bring it down a
    part: audio alone (a podcast is its sound), a container that doesn't say
    how big its sound is, and a file that would need just as many messages
    afterwards are all left as they are. The picture is copied, not re-encoded.

    The new file keeps ``path``'s name, so ``into`` has to be some other
    directory than the one ``path`` is in.

    Nothing that goes wrong with the file is raised. This is a nicety on the
    way to :func:`split`, which still takes the file as it is: an encode that
    fails or comes out no smaller is noted in the log and costs nothing but its
    own time.
    """
    name = os.path.basename(path)
    out = os.path.join(into, name)
    if os.path.abspath(out) == os.path.abspath(path):
        raise ValueError("slim would write over the file it was given")
    if os.path.splitext(name)[1].lower() not in _FASTSTART_EXTS:
        return None
    size = os.path.getsize(path)
    parts = parts_needed(size, max_bytes)

    try:
        sound = _sound(path, timeout=timeout)
        if not sound:
            return None
        silent = size - sum(rate * seconds / 8 for rate, seconds in sound)
        heard = sum(seconds for _, seconds in sound)
        for bitrate in SLIM_AUDIO_BITRATES:
            predicted = (silent + bitrate * heard / 8) * SLIM_MARGIN
            if parts_needed(int(predicted), max_bytes) < parts:
                break
        else:
            return None

        cmd = [
            _binary("ffmpeg"),
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-i",
            path,
            "-map",
            "0:V",
            "-map",
            "0:a",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            str(bitrate),
            "-movflags",
            "+faststart",
            out,
        ]
        with timing.step("slim audio", f"{bitrate // 1000} kbps"):
            _run(cmd, timeout=timeout, what="ffmpeg")
        if parts_needed(os.path.getsize(out), max_bytes) >= parts:
            raise FeedError(f"still {parts} parts at {bitrate // 1000} kbps")
    except (FeedError, OSError) as exc:
        log_line(f"parts: sound of {name} not slimmed: {exc}")
        if os.path.exists(out):
            os.unlink(out)
        return None
    return out


def _sound(path: str, *, timeout: float) -> list[tuple[float, float]] | None:
    """``(bits per second, seconds)`` for each audio stream of a video.

    ``None`` if ``path`` has no picture — cover art doesn't count, for the
    reason given in :func:`_segment` — or doesn't state both figures for every
    audio stream, which mp4 does and some other containers don't.
    """
    result = _run(
        [
            _binary("ffprobe"),
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,bit_rate,duration:stream_disposition=attached_pic",
            "-of",
            "json",
            path,
        ],
        timeout=timeout,
        what="ffprobe",
    )
    try:
        streams = json.loads(result.stdout)["streams"]
        picture = any(
            s["codec_type"] == "video" and not s["disposition"]["attached_pic"]
            for s in streams
        )
        sound = [
            (float(s["bit_rate"]), float(s["duration"]))
            for s in streams
            if s["codec_type"] == "audio"
        ]
    except (ValueError, KeyError, TypeError):
        return None
    return sound if picture else None


def split(
    path: str,
    into: str,
    *,
    max_bytes: int = MAX_BYTES,
    max_parts: int = MAX_PARTS,
    timeout: float = SPLIT_TIMEOUT,
) -> list[str]:
    """Cut ``path`` into the fewest parts under ``max_bytes``, written to ``into``."""
    size = os.path.getsize(path)
    parts = parts_needed(size, max_bytes)
    if parts > max_parts:
        raise too_big("File", size, max_bytes=max_bytes)

    duration = _duration(path, timeout=timeout)
    while parts <= max_parts:
        pieces = _segment(path, into, parts, duration, timeout=timeout)
        if all(os.path.getsize(p) <= max_bytes for p in pieces):
            return pieces
        # A cut snapped far enough from the middle that one side is over. One
        # part finer moves every cut, and the sizes with them.
        for piece in pieces:
            os.unlink(piece)
        parts += 1
    raise too_big("File", size, max_bytes=max_bytes)


def _duration(path: str, *, timeout: float) -> float:
    """The length of ``path`` in seconds, as ffprobe reads it."""
    result = _run(
        [
            _binary("ffprobe"),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        timeout=timeout,
        what="ffprobe",
    )
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise FeedError(f"ffprobe gave no duration for {os.path.basename(path)}") from exc
    if duration <= 0:
        raise FeedError(f"ffprobe gave no duration for {os.path.basename(path)}")
    return duration


def _segment(
    path: str, into: str, parts: int, duration: float, *, timeout: float
) -> list[str]:
    """Stream-copy ``path`` into ``parts`` pieces of equal length, in order.

    ``0:V?`` is the video, if there is any, *without* attached pictures: an mp3
    with its cover art embedded carries the art as a video stream, which the
    segment muxer would try to cut too. ``0:a`` is every audio stream.
    """
    stem, ext = os.path.splitext(os.path.basename(path))
    ext = ext.lower() or ".mp3"
    cuts = ",".join(f"{duration * k / parts:.3f}" for k in range(1, parts))
    pattern = os.path.join(into, f"{stem}.part%d{ext}")

    cmd = [
        _binary("ffmpeg"),
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-i",
        path,
        "-map",
        "0:V?",
        "-map",
        "0:a",
        "-c",
        "copy",
        "-f",
        "segment",
        "-segment_times",
        cuts,
        "-segment_start_number",
        "1",
        "-reset_timestamps",
        "1",
    ]
    if ext in _FASTSTART_EXTS:
        cmd += ["-segment_format_options", "movflags=+faststart"]
    cmd.append(pattern)

    with timing.step("split", f"{parts} parts"):
        _run(cmd, timeout=timeout, what="ffmpeg")

    numbered = re.compile(rf"^{re.escape(stem)}\.part(\d+){re.escape(ext)}$")
    found = sorted(
        (int(m[1]), os.path.join(into, name))
        for name in os.listdir(into)
        if (m := numbered.match(name))
    )
    pieces = [piece for _, piece in found]
    if not pieces or any(os.path.getsize(p) == 0 for p in pieces):
        raise FeedError("ffmpeg produced no parts")
    return pieces


def _binary(name: str) -> str:
    binary = shutil.which(name)
    if binary is None:
        raise FeedError(
            f"{name} is needed to split a file too big for one Signal message "
            "but was not found on PATH (e.g. `brew install ffmpeg`)"
        )
    return binary


def _run(cmd: list[str], *, timeout: float, what: str) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise FeedError(f"{what} timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        raise FeedError(f"Could not run {what}: {exc}") from exc
    if result.returncode != 0:
        lines = [line for line in (result.stderr or "").splitlines() if line.strip()]
        raise FeedError(f"{what} failed: {lines[-1] if lines else 'no error output'}")
    return result
