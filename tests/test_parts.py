"""Tests for rssignal.parts: the fewest-parts rule, and cutting files to fit.

ffmpeg and ffprobe are faked except in the one test at the bottom that makes a
real file and cuts it, which is skipped on a machine without ffmpeg.
"""

import os
import shutil
import subprocess

import pytest

from rssignal import parts
from rssignal.feeds import FeedError
from rssignal.video import VideoTooBig

MB = 1_000_000


def _choose(sizes_mb, max_mb=95):
    return parts.choose(
        sizes_mb,
        size=lambda s: s * MB,
        rank=lambda s: s,
        max_bytes=max_mb * MB,
    )


# --- choosing -------------------------------------------------------------


def test_parts_needed_rounds_up_and_never_says_zero():
    assert parts.parts_needed(0, 95) == 1
    assert parts.parts_needed(95, 95) == 1
    assert parts.parts_needed(96, 95) == 2
    assert parts.parts_needed(190, 95) == 2
    assert parts.parts_needed(191, 95) == 3


def test_choose_takes_the_best_that_fits_in_one_when_anything_does():
    assert _choose([40, 90, 180, 400]) == (90, 1)


def test_choose_takes_the_fewest_parts_then_the_best_in_them():
    # The smallest is 105 MB, so two parts; two parts' 190 MB takes the 180,
    # not the 400 that three or more parts would have allowed.
    assert _choose([105, 180, 250, 400]) == (180, 2)


def test_choose_gives_up_past_the_most_parts():
    assert _choose([parts.MAX_PARTS * 95 + 1]) is None


def test_choose_with_nothing_to_choose_from():
    assert _choose([]) is None


def test_in_parts_says_nothing_for_one():
    assert parts.in_parts(1) == ""
    assert parts.in_parts(3) == ", in 3 parts"


# --- splitting, with ffmpeg faked -----------------------------------------


def _file(tmp_path, name, size):
    path = tmp_path / name
    path.write_bytes(b"x" * size)
    return str(path)


def _fake_tools(monkeypatch, *, duration="600.0", part_sizes=None):
    """Fake ffprobe (a duration) and ffmpeg (parts of the given sizes).

    ``part_sizes`` maps a part count to the sizes of the parts ffmpeg writes for
    it; by default each part is a tenth of a byte under the limit per part.
    """
    calls = []
    monkeypatch.setattr(parts.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0].endswith("ffprobe"):
            return subprocess.CompletedProcess(cmd, 0, duration + "\n", "")
        pattern = cmd[-1]
        cuts = cmd[cmd.index("-segment_times") + 1]
        count = cuts.count(",") + 2 if cuts else 1
        for k, size in enumerate((part_sizes or {}).get(count, [10] * count), start=1):
            with open(pattern.replace("%d", str(k)), "wb") as fh:
                fh.write(b"x" * size)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(parts.subprocess, "run", fake_run)
    return calls


def test_split_temp_leaves_a_file_that_fits_alone(tmp_path, monkeypatch):
    calls = _fake_tools(monkeypatch)
    path = _file(tmp_path, "ep.mp3", 100)

    with parts.split_temp(path, max_bytes=100) as pieces:
        assert pieces == [path]

    assert calls == []
    assert os.path.exists(path)


def test_split_cuts_into_as_many_parts_as_the_size_needs(tmp_path, monkeypatch):
    calls = _fake_tools(monkeypatch)
    path = _file(tmp_path, "ep.mp3", 250)
    into = tmp_path / "out"
    into.mkdir()

    pieces = parts.split(path, str(into), max_bytes=100)

    assert [os.path.basename(p) for p in pieces] == [
        "ep.part1.mp3",
        "ep.part2.mp3",
        "ep.part3.mp3",
    ]
    ffmpeg = calls[-1]
    # Equal lengths of the 600 s the probe reported.
    assert ffmpeg[ffmpeg.index("-segment_times") + 1] == "200.000,400.000"
    assert "-c" in ffmpeg and ffmpeg[ffmpeg.index("-c") + 1] == "copy"
    # An mp3 has no index to move to the front.
    assert "-segment_format_options" not in ffmpeg


def test_split_asks_for_faststart_in_an_mp4(tmp_path, monkeypatch):
    calls = _fake_tools(monkeypatch)
    path = _file(tmp_path, "video.mp4", 150)
    into = tmp_path / "out"
    into.mkdir()

    pieces = parts.split(path, str(into), max_bytes=100)

    assert all(p.endswith(".mp4") for p in pieces)
    assert "movflags=+faststart" in calls[-1]


def test_split_goes_one_part_finer_when_a_cut_lands_badly(tmp_path, monkeypatch):
    # Two parts would be enough by size, but the keyframe the cut snapped to
    # left one side over the limit. Three are tried next.
    _fake_tools(monkeypatch, part_sizes={2: [120, 30], 3: [60, 50, 40]})
    path = _file(tmp_path, "video.mp4", 150)
    into = tmp_path / "out"
    into.mkdir()

    pieces = parts.split(path, str(into), max_bytes=100)

    assert len(pieces) == 3
    assert sorted(os.listdir(into)) == [os.path.basename(p) for p in pieces]


def test_split_gives_up_past_the_most_parts(tmp_path, monkeypatch):
    calls = _fake_tools(monkeypatch)
    path = _file(tmp_path, "ep.mp3", 500)

    with pytest.raises(VideoTooBig, match="too big to send"):
        parts.split(path, str(tmp_path), max_bytes=100, max_parts=4)

    # Decided from the size alone, without running anything.
    assert calls == []


def test_split_temp_removes_its_parts_afterwards(tmp_path, monkeypatch):
    _fake_tools(monkeypatch)
    path = _file(tmp_path, "ep.mp3", 150)

    with parts.split_temp(path, max_bytes=100) as pieces:
        assert all(os.path.exists(p) for p in pieces)

    assert not any(os.path.exists(p) for p in pieces)
    # The original is the caller's.
    assert os.path.exists(path)


def test_split_without_ffmpeg_says_how_to_get_it(tmp_path, monkeypatch):
    monkeypatch.setattr(parts.shutil, "which", lambda name: None)
    path = _file(tmp_path, "ep.mp3", 150)

    with pytest.raises(FeedError, match="brew install ffmpeg"):
        parts.split(path, str(tmp_path), max_bytes=100)


def test_split_reports_what_ffmpeg_said(tmp_path, monkeypatch):
    monkeypatch.setattr(parts.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(cmd, **kwargs):
        if cmd[0].endswith("ffprobe"):
            return subprocess.CompletedProcess(cmd, 0, "600\n", "")
        return subprocess.CompletedProcess(cmd, 1, "", "noise\nInvalid data found\n")

    monkeypatch.setattr(parts.subprocess, "run", fake_run)
    path = _file(tmp_path, "ep.mp3", 150)

    with pytest.raises(FeedError, match="ffmpeg failed: Invalid data found"):
        parts.split(path, str(tmp_path), max_bytes=100)


def test_split_without_a_duration_fails(tmp_path, monkeypatch):
    _fake_tools(monkeypatch, duration="N/A")
    path = _file(tmp_path, "ep.mp3", 150)

    with pytest.raises(FeedError, match="no duration"):
        parts.split(path, str(tmp_path), max_bytes=100)


# --- the real thing -------------------------------------------------------


@pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="needs ffmpeg and ffprobe",
)
def test_split_really_cuts_an_mp3_into_playable_parts(tmp_path):
    source = tmp_path / "tone.mp3"
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi",
            "-i", "sine=frequency=440:duration=30", "-b:a", "64k", str(source),
        ],
        check=True,
    )
    size = os.path.getsize(source)

    with parts.split_temp(str(source), max_bytes=size // 2 + 1024) as pieces:
        assert len(pieces) == 2
        durations = [
            float(
                subprocess.run(
                    [
                        "ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", piece,
                    ],
                    capture_output=True, text=True, check=True,
                ).stdout
            )
            for piece in pieces
        ]
        assert all(os.path.getsize(p) <= size // 2 + 1024 for p in pieces)

    assert sum(durations) == pytest.approx(30, abs=0.5)
