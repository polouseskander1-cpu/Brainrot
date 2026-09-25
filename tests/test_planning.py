import random
from pathlib import Path
from types import SimpleNamespace

import pytest

from brainrot_bot.config import DEFAULTS
from brainrot_bot.gameplay import SAFETY, Footage, pick_music, plan_segments
from brainrot_bot.media import MediaError
from brainrot_bot.parts import plan_parts
from brainrot_bot.render import RenderJob, build_command, compute_layout
from brainrot_bot.transcribe import Word


def footage(name, duration):
    return Footage(Path(name), name, duration)


def test_single_long_recording_with_random_start():
    rng = random.Random(1)
    library = [footage("a.mp4", 600)]
    for _ in range(50):
        (seg,) = plan_segments(library, 60, {}, rng)
        assert seg.duration == pytest.approx(60 + SAFETY)
        assert 0 <= seg.start <= 600 - 60 - SAFETY


def test_least_used_recording_is_picked_first():
    library = [footage("a.mp4", 300), footage("b.mp4", 300), footage("c.mp4", 300)]
    usage = {"a.mp4": 3, "b.mp4": 1, "c.mp4": 3}
    for seed in range(20):
        (seg,) = plan_segments(library, 30, usage, random.Random(seed))
        assert seg.key == "b.mp4"


def test_only_recordings_long_enough_are_used_alone():
    library = [footage("short.mp4", 20), footage("long.mp4", 200)]
    for seed in range(20):
        segments = plan_segments(library, 60, {"long.mp4": 50}, random.Random(seed))
        assert [s.key for s in segments] == ["long.mp4"]


def test_short_recordings_are_chained_to_cover_the_clip():
    library = [footage("a.mp4", 12), footage("b.mp4", 9.5), footage("c.mp4", 30)]
    for seed in range(30):
        segments = plan_segments(library, 95, {}, random.Random(seed))
        assert sum(s.duration for s in segments) >= 95
        assert all(s.duration >= 0.25 for s in segments)
        for s in segments:
            source = next(f for f in library if f.key == s.key)
            assert 0 <= s.start and s.start + s.duration <= source.duration + 1e-6
        # the same recording is not used twice in a row when there is a choice
        assert all(x.key != y.key for x, y in zip(segments, segments[1:]))


def test_skip_start_and_end():
    library = [footage("a.mp4", 100)]
    for seed in range(30):
        (seg,) = plan_segments(library, 20, {}, random.Random(seed), skip_start=10, skip_end=10)
        assert seg.start >= 10 and seg.start + seg.duration <= 90 + 1e-6


def test_empty_library():
    with pytest.raises(MediaError):
        plan_segments([], 10, {})


def test_music_pick():
    assert pick_music([], 10) is None
    track, start = pick_music([footage("m.mp3", 200)], 30, random.Random(3))
    assert 0 <= start <= 170


def test_parts_off_or_short():
    assert plan_parts(50, [], 0) == [(0.0, 50)]
    assert plan_parts(50, [], 60) == [(0.0, 50)]


def test_parts_cut_at_pauses():
    # A word every 0.5s, with a sentence-ending pause from 27.9s to 28.7s and from 60.9s to 61.7s.
    ws = []
    t = 0.0
    while t < 90:
        text = "end." if abs(t - 27.5) < 0.01 or abs(t - 60.5) < 0.01 else "word"
        ws.append(Word(text, t, t + 0.4))
        t += 0.5 if text == "word" else 1.2
    parts = plan_parts(90, ws, 35)
    assert len(parts) == 3
    assert parts[0][0] == 0 and parts[-1][1] == 90
    for (a, b), (c, _) in zip(parts, parts[1:]):
        assert b == c
    assert all(end - start <= 35 for start, end in parts)
    assert parts[0][1] == pytest.approx(28.3, abs=0.01)  # the middle of the long pause after "end."


def video_cfg(**overrides):
    values = dict(DEFAULTS["video"])
    values.update(overrides)
    return SimpleNamespace(**values)


def test_layout_snaps_16x9_into_top_third():
    layout = compute_layout(video_cfg(), 1920, 1080)
    assert (layout.top_h, layout.fit, layout.game_h) == (608, False, 1312)


def test_layout_fits_other_shapes_without_cropping():
    for w, h in ((1080, 1920), (1440, 1080), (1080, 1080)):
        layout = compute_layout(video_cfg(), w, h)
        assert layout.top_h == 640 and layout.fit
    assert compute_layout(video_cfg(snap_top=False), 1920, 1080).fit


def full_cfg():
    return SimpleNamespace(**{k: SimpleNamespace(**v) for k, v in DEFAULTS.items()})


def make_job(**overrides):
    from brainrot_bot.gameplay import Segment

    values = dict(
        clip=Path("clip.mp4"),
        clip_start=0.0,
        duration=30.0,
        clip_has_audio=True,
        layout=compute_layout(video_cfg(), 1920, 1080),
        segments=[Segment(Path("g.mp4"), "g.mp4", 12.5, 30.25)],
        output=Path("out.mp4"),
        ass_file=".work/x/part1.ass",
    )
    values.update(overrides)
    return RenderJob(**values)


def test_command_single_segment():
    args = build_command(make_job(), full_cfg())
    graph = args[args.index("-filter_complex") + 1]
    assert args[:6] == ["-t", "30.000", "-i", "clip.mp4", "-ss", "12.500"]
    assert "vstack=inputs=2" in graph and "concat" not in graph
    assert "ass=filename=.work/x/part1.ass:fontsdir=fonts" in graph
    assert "loudnorm=I=-14" in graph and "acompressor" in graph
    assert "overlay=x='-w+w*t/30.000'" in graph  # progress bar
    assert args[args.index("-maxrate") + 1] == "12M" and args[args.index("-bufsize") + 1] == "24M"
    assert args[-1] == "out.mp4"
    assert args[args.index("-map") + 1] == "[vout]"


def test_command_chained_gameplay_music_and_silent_clip():
    from brainrot_bot.gameplay import Segment

    segs = [Segment(Path("a.mp4"), "a.mp4", 0, 10), Segment(Path("b.mp4"), "b.mp4", 3, 10), Segment(Path("a.mp4"), "a.mp4", 0, 10.25)]
    job = make_job(segments=segs, clip_has_audio=False, music=Path("m.mp3"), music_start=5.0, music_loop=True, clip_start=42.0, ass_file=None)
    args = build_command(job, full_cfg())
    graph = args[args.index("-filter_complex") + 1]
    assert args[:2] == ["-ss", "42.000"]
    assert "concat=n=3:v=1:a=0" in graph
    assert args.count("-stream_loop") == 1 and "anullsrc=r=48000:cl=stereo" in args
    assert "[4:a:0]atrim" in graph and "[5:a]" in graph  # music is input 4, silence input 5
    assert "amix=inputs=2" in graph and "loudnorm" not in graph and "ass=" not in graph
