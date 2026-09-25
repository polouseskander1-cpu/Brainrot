"""The editing features: keywords, emojis, censoring, pause cutting, zooms, sound effects, styles, layouts."""

import re
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from brainrot_bot.analysis import (
    Loudness,
    TimeMap,
    censor_word,
    emoji_for,
    emoji_path,
    is_keyword,
    is_profane,
    speech_intervals,
)
from brainrot_bot.captions import build_ass, build_srt, emoji_events, make_chunks
from brainrot_bot.config import DEFAULTS, FONTS_DIR
from brainrot_bot.editor import ZOOM_GAP, plan_edit
from brainrot_bot.faces import Face, face_at, track_x
from brainrot_bot.gameplay import Segment
from brainrot_bot.render import (
    EmojiShow,
    RenderJob,
    Zoom,
    _linear,
    _piecewise,
    build_command,
    compute_layout,
    encoder_args,
)
from brainrot_bot.sfx import RATE, SoundEvent, write_track
from brainrot_bot.styles import DESCRIPTIONS, PRESETS, apply_style
from brainrot_bot.thumbnail import cover_ass
from brainrot_bot.transcribe import Word


def full_cfg(**sections):
    cfg = SimpleNamespace(**{k: SimpleNamespace(**v) for k, v in DEFAULTS.items()})
    for section, values in sections.items():
        for key, value in values.items():
            setattr(getattr(cfg, section), key, value)
    return cfg


def evaluate(expr: str, t: float) -> float:
    """Evaluate one of our ffmpeg expressions at time t (they only use if/between/lt and arithmetic)."""
    py = expr.replace("if(", "_if(").replace("between(", "_between(").replace("lt(", "_lt(")
    return eval(py, {"_if": lambda c, a, b: a if c else b, "_between": lambda x, a, b: a <= x <= b,
                     "_lt": lambda a, b: a < b, "t": t})


# ------------------------------------------------------------------ words


def test_keywords():
    for word in ("MONEY!", "$500", "never", "craziest", "40%", "million", "Secret,"):
        assert is_keyword(word), word
    for word in ("the", "and", "honest", "table", "", "...", "interest", "west"):
        assert not is_keyword(word), word


def test_emojis_exist_for_their_words():
    assert emoji_for("Money,") == "1f4b0" and emoji_for("FIRE") == "1f525"
    assert emoji_for("table") is None
    assert emoji_path("1f4b0").exists()
    from brainrot_bot.analysis import EMOJI_WORDS

    missing = [code for code in EMOJI_WORDS if not emoji_path(code).exists()]
    assert not missing, f"emoji pictures missing: {missing}"


def test_swear_words():
    assert is_profane("Fucking!") and is_profane("SHIT") and is_profane("bullshit.")
    assert not is_profane("hello") and not is_profane("")
    assert is_profane("Frick", {"frick"}) and not is_profane("Frick")
    assert censor_word("FUCKING") == "F******"
    assert censor_word("shit!") == "s***!"
    assert censor_word("a") == "a"


def test_captions_mark_keywords_emojis_and_censor():
    cfg = apply_style(full_cfg().captions)
    ws = [Word("This", 0, 0.2), Word("fucking", 0.25, 0.6), Word("money", 0.65, 1.0), Word("thing.", 1.05, 1.3)]
    chunks = make_chunks(ws, cfg, censor=set())
    flat = [w for c in chunks for w in c.words]
    assert [w.text for w in flat] == ["THIS", "F******", "MONEY", "THING"]
    assert [w.keyword for w in flat] == [False, False, True, False]
    assert flat[2].emoji == "1f4b0" and flat[1].censored and flat[1].emoji is None
    ass = build_ass(chunks, width=1080, height=1920, duration=2, captions=cfg)
    assert "\\1c&H6AFF39&}MONEY" in ass  # keyword color #39FF6A
    assert "fucking" not in ass.lower()
    assert "f******" in build_srt(ws, censor=set()).lower()
    assert "fucking" in build_srt(ws).lower()  # censoring off


def test_emoji_events_are_spaced_out():
    cfg = apply_style(full_cfg().captions)
    ws = [Word("money", 0, 0.4), Word("fire.", 0.5, 0.9), Word("money.", 1.5, 1.9), Word("fire.", 5.0, 5.4)]
    events = emoji_events(make_chunks(ws, cfg, total=6))
    assert [code for code, _, _ in events] == ["1f4b0", "1f525"]  # the second one was too soon after the first
    assert all(b - a >= 0.6 for _, a, b in events)


def test_highlight_and_karaoke_colors():
    cfg = apply_style(full_cfg(captions={"style": "karaoke"}).captions)
    chunks = make_chunks([Word("one", 0, 0.3), Word("two", 0.4, 0.7)], cfg)
    ass = build_ass(chunks, width=1080, height=1920, duration=1, captions=cfg)
    # each word turns yellow when it is said and stays yellow (karaoke)
    assert "\\t(0,1,\\1c&H00D4FF&)}ONE" in ass and "\\t(400,401,\\1c&H00D4FF&)}TWO" in ass
    assert ass.count("\\t(") >= 2 and "\\1c&HFFFFFF&\\t(400,401,\\1c&H00D4FF&)" in ass


# ------------------------------------------------------------------ pauses


def test_speech_intervals_cut_long_pauses_and_trim_the_ends():
    ws = [Word("a", 1.0, 1.4), Word("b", 1.5, 1.9), Word("c", 4.0, 4.5)]
    kept = speech_intervals(ws, 0, 6, max_pause=0.45, pad=0.12, fps=100)
    assert kept == pytest.approx([(0.88, 2.02), (3.88, 4.74)])
    # a short pause is not cut
    assert len(speech_intervals(ws, 0, 6, max_pause=3, fps=100)) == 1
    # no words: everything is kept
    assert speech_intervals([], 2, 5, fps=30) == [(2, 5)]


def test_loud_pauses_are_kept():
    ws = [Word("a", 1.0, 1.4), Word("c", 4.0, 4.5)]
    laughing = Loudness([0.1] * 60)
    assert len(speech_intervals(ws, 0, 6, fps=100, loudness=laughing)) == 1
    quiet_gap = Loudness([0.1] * 15 + [0.001] * 25 + [0.1] * 20)
    assert len(speech_intervals(ws, 0, 6, fps=100, loudness=quiet_gap)) == 2


def test_intervals_are_frame_exact():
    ws = [Word("a", 1.013, 1.4), Word("c", 4.07, 4.5)]
    for a, b in speech_intervals(ws, 0, 6, fps=30):
        assert round(a * 30, 6) == round(a * 30) and round(b * 30, 6) == round(b * 30)


def test_time_map():
    tm = TimeMap([(1.0, 2.0), (3.0, 4.5)])
    assert tm.duration == 2.5
    assert tm.to_output(1.5) == pytest.approx(0.5)
    assert tm.to_output(3.0) == pytest.approx(1.0) and tm.to_output(4.0) == pytest.approx(2.0)
    assert tm.to_output(2.5) == pytest.approx(1.0)  # inside a cut: the cut point
    assert tm.to_source(0.5) == pytest.approx(1.5) and tm.to_source(1.2) == pytest.approx(3.2)
    mapped = tm.map_words([Word("x", 0.2, 0.5), Word("y", 1.2, 1.4), Word("z", 3.5, 3.9)])
    assert [(w.text, round(w.start, 3), round(w.end, 3)) for w in mapped] == [("y", 0.2, 0.4), ("z", 1.5, 1.9)]


def test_loudness_from_wav(tmp_path):
    rate = 16000
    t = np.arange(rate * 2) / rate
    samples = np.where(t < 1, 0.3 * np.sin(2 * np.pi * 200 * t), 0.0)
    path = tmp_path / "v.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((samples * 32767).astype("<i2").tobytes())
    loud = Loudness.from_wav(path)
    assert len(loud.levels) == 20
    assert loud.median == pytest.approx(0.3 / np.sqrt(2), rel=0.05)
    assert loud.is_quiet(1.1, 1.9) and not loud.is_quiet(0.2, 0.8)
    assert loud.boost(0.2, 0.8) == 0.0


# ------------------------------------------------------------------ edit plan

TALK = [
    Word(text, start, start + 0.35)
    for text, start in [
        ("So", 0.5), ("this", 0.9), ("is", 1.3), ("the", 1.7), ("biggest", 2.1), ("secret", 2.5), ("about", 2.9),
        ("money.", 3.3), ("Most", 6.0), ("people", 6.4), ("never", 6.8), ("learn", 7.2), ("it.", 7.6),
        ("They", 8.2), ("waste", 8.6), ("time", 9.0), ("and", 9.4), ("fucking", 9.8), ("cash.", 10.2),
        ("It", 10.9), ("is", 11.3), ("insane!", 11.7), ("Really", 14.0), ("crazy", 14.4), ("stuff.", 14.8),
    ]
]


def split_layout(cfg):
    return compute_layout(cfg.video, 1920, 1080)


def test_plan_cuts_pauses_and_moves_everything_to_reel_time():
    cfg = full_cfg()
    plan = plan_edit(cfg, split_layout(cfg), 0, 16, TALK)
    assert len(plan.intervals) == 3  # pauses at 3.65-6.0 and 12.05-14.0 are cut
    assert plan.duration == pytest.approx(sum(b - a for a, b in plan.intervals))
    assert plan.duration < 13
    assert plan.words[0].start == pytest.approx(0.12, abs=0.04)  # leading silence trimmed
    assert all(0 <= w.start < w.end <= plan.duration for w in plan.words)
    assert all(c.end <= plan.duration + 1e-6 for c in plan.chunks)


def test_plan_zooms_on_strong_moments_with_sounds():
    cfg = full_cfg()
    plan = plan_edit(cfg, split_layout(cfg), 0, 16, TALK)
    assert 1 <= len(plan.zooms) <= max(1, int(plan.duration / 6))
    for a, b in zip(plan.zooms, plan.zooms[1:]):
        assert b.start - a.start >= ZOOM_GAP
    kinds = [s.kind for s in plan.sounds]
    assert kinds.count("whoosh") == len(plan.zooms)
    whooshes = sorted(s.at for s in plan.sounds if s.kind == "whoosh")
    assert whooshes == pytest.approx([max(0, z.start - 0.12) for z in plan.zooms])
    assert plan.emojis and kinds.count("pop") == len(plan.emojis)
    assert all(e.path.exists() for e in plan.emojis)
    assert plan.zooms[0].start < plan.cover_at < plan.zooms[0].end


def test_plan_censors_with_bleep_or_mute():
    cfg = full_cfg(edit={"censor": True})
    plan = plan_edit(cfg, split_layout(cfg), 0, 16, TALK)
    (mute,) = plan.mutes
    word = next(w for w in plan.words if w.text == "fucking")
    assert mute[0] < word.start and mute[1] > word.end
    bleep = next(s for s in plan.sounds if s.kind == "bleep")
    assert bleep.at == pytest.approx(mute[0]) and bleep.length == pytest.approx(mute[1] - mute[0])
    assert "F******" in [w.text for c in plan.chunks for w in c.words]

    cfg = full_cfg(edit={"censor": True, "censor_mode": "mute"})
    plan = plan_edit(cfg, split_layout(cfg), 0, 16, TALK)
    assert len(plan.mutes) == 1 and not [s for s in plan.sounds if s.kind == "bleep"]


def test_plan_with_everything_off():
    cfg = full_cfg(edit={"cut_silences": False, "zoom": False, "sfx": False, "emojis": False})
    plan = plan_edit(cfg, split_layout(cfg), 2, 12, TALK)
    assert plan.intervals == [(2, 12)] and plan.duration == 10
    assert not plan.zooms and not plan.sounds and not plan.emojis and not plan.mutes
    assert plan.cover_at == pytest.approx(3.0)
    cfg = full_cfg(captions={"enabled": False})
    assert plan_edit(cfg, split_layout(cfg), 0, 16, TALK).chunks == []


def test_plan_zoom_aims_at_the_face():
    cfg = full_cfg(edit={"cut_silences": False})
    faces = [Face(t / 2, 0.8, 0.3, 0.1) for t in range(32)]
    plan = plan_edit(cfg, split_layout(cfg), 0, 16, TALK, faces=faces)
    assert plan.zooms and all((z.cx, z.cy) == (0.8, 0.3) for z in plan.zooms)
    cfg.edit.face_tracking = False
    plan = plan_edit(cfg, split_layout(cfg), 0, 16, TALK, faces=faces)
    assert all(z.cx == 0.5 for z in plan.zooms)


def test_plan_fullscreen_follows_the_face():
    cfg = full_cfg(video={"layout": "fullscreen"}, edit={"cut_silences": False})
    faces = [Face(t / 2, 0.75 if t < 16 else 0.25, 0.4, 0.1) for t in range(32)]
    plan = plan_edit(cfg, compute_layout(cfg.video, 1920, 1080), 0, 16, TALK, faces=faces)
    xs = dict(plan.reframe)
    assert xs[0.0] == pytest.approx(0.75) and xs[15.0] == pytest.approx(0.25)


def test_track_x_smooths_small_moves_and_jumps_on_speaker_change():
    faces = [Face(0, 0.5, 0.4, 0.1), Face(1, 0.55, 0.4, 0.1), Face(2, 0.2, 0.4, 0.1)]
    points = dict(track_x(faces, 0, 2))
    assert points[0] == 0.5
    assert 0.5 < points[1] < 0.55  # eased towards the small move
    assert points[2] == 0.2  # jumped to the other speaker
    assert face_at(faces, 10) is None and face_at(faces, 1.2).cx == 0.55


# ------------------------------------------------------------------ sound effects


def test_sound_effects_track(tmp_path):
    events = [SoundEvent("whoosh", 0.5), SoundEvent("boom", 1.5), SoundEvent("pop", 3.0), SoundEvent("bleep", 3.5, 0.4),
              SoundEvent("unknown", 1.0), SoundEvent("boom", 9.9)]
    path = write_track(events, 4.2, tmp_path / "sfx.wav", volume=0.6)
    with wave.open(str(path), "rb") as handle:
        assert (handle.getnchannels(), handle.getframerate()) == (2, RATE)
        assert handle.getnframes() == int(4.2 * RATE)
        data = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2").reshape(-1, 2)[:, 0] / 32768

    def rms(a, b):
        return float(np.sqrt((data[int(a * RATE):int(b * RATE)] ** 2).mean()))

    assert rms(0.0, 0.45) < 1e-4  # nothing before the whoosh
    assert rms(0.55, 0.9) > 0.02 and rms(1.5, 1.8) > 0.1 and rms(3.0, 3.08) > 0.02
    bleep = data[int(3.55 * RATE):int(3.85 * RATE)]
    spectrum = np.abs(np.fft.rfft(bleep))
    assert np.fft.rfftfreq(len(bleep), 1 / RATE)[spectrum.argmax()] == pytest.approx(1000, abs=5)


# ------------------------------------------------------------------ styles


def test_styles():
    base = full_cfg().captions
    assert vars(apply_style(base)) == vars(base)  # classic = your own settings
    for name, preset in PRESETS.items():
        base.style = name
        styled = apply_style(base)
        for key, value in preset.items():
            assert getattr(styled, key) == value
        assert name in DESCRIPTIONS
    assert base.font == DEFAULTS["captions"]["font"]  # the original settings are not changed


def test_style_fonts_are_bundled():
    files = {p.name.lower().replace("-", "").replace("regular", "") for p in FONTS_DIR.glob("*.ttf")}
    for name, preset in PRESETS.items():
        font = preset.get("font", DEFAULTS["captions"]["font"]).lower().replace(" ", "")
        assert f"{font}.ttf" in files, (name, font)


def test_cover_title():
    ass = cover_ass("the fall of rome explained in sixty seconds", 1080, 1920, full_cfg().captions)
    text = ass.strip().splitlines()[-1]
    assert "THE FALL OF\\NROME EXPLAINED\\NIN SIXTY\\NSECONDS" in text
    assert "Montserrat Black" in ass and "\\pos(540,845)" in ass
    assert "WATCH THIS" in cover_ass("   ", 1080, 1920, full_cfg().captions)


# ------------------------------------------------------------------ layouts and the ffmpeg command


def video_cfg(**overrides):
    values = dict(DEFAULTS["video"])
    values.update(overrides)
    return SimpleNamespace(**values)


def test_layouts():
    floating = compute_layout(video_cfg(layout="floating"), 1920, 1080)
    x, y, w, h = floating.clip_box
    assert floating.game_box == (0, 0, 1080, 1920)
    assert w <= 1080 * 0.9 + 1 and x == (1080 - w) // 2 and y > 0
    assert floating.clip_size[0] / floating.clip_size[1] == pytest.approx(16 / 9, rel=0.01)
    assert floating.seam_y == y + h + 16

    full = compute_layout(video_cfg(layout="fullscreen"), 1920, 1080)
    assert full.game_box is None and full.clip_box == (0, 0, 1080, 1920)

    side = compute_layout(video_cfg(layout="side", width=1920, height=1080), 1080, 1920)
    assert side.clip_box == (0, 0, 960, 1080) and side.game_box == (960, 0, 960, 1080)
    assert side.clip_size == (608, 1080) and side.fit


def make_job(**overrides):
    values = dict(
        clip=Path("clip.mp4"),
        clip_start=10.0,
        duration=4.5,
        clip_has_audio=True,
        layout=compute_layout(video_cfg(), 1920, 1080),
        segments=[Segment(Path("g.mp4"), "g.mp4", 12.5, 4.75)],
        output=Path("out.mp4"),
    )
    values.update(overrides)
    return RenderJob(**values)


def graph_of(args):
    return args[args.index("-filter_complex") + 1]


def test_command_joins_the_kept_parts_exactly():
    job = make_job(intervals=[(10.0, 12.0), (13.0, 15.5)])
    args = build_command(job, full_cfg())
    graph = graph_of(args)
    assert args[:6] == ["-ss", "10.000", "-t", "5.700", "-i", "clip.mp4"]
    assert "trim=start_frame=0:end_frame=60" in graph and "trim=start_frame=90:end_frame=165" in graph
    assert "atrim=start_sample=0:end_sample=96000" in graph and "atrim=start_sample=144000:end_sample=264000" in graph
    assert "concat=n=2:v=1:a=1" in graph and "tpad=stop_mode=clone" in graph
    assert args[args.index("-t", args.index("-map")) + 1] == "4.500"


@pytest.mark.parametrize("mode,marker", [("floating", "overlay=x="), ("side", "hstack=inputs=2"), ("split", "vstack=inputs=2")])
def test_command_layouts(mode, marker):
    video = video_cfg(layout=mode)
    job = make_job(layout=compute_layout(video, 1920, 1080), clip_size=(1920, 1080))
    args = build_command(job, full_cfg(video={"layout": mode}))
    assert marker in graph_of(args) and "g.mp4" in args


def test_command_fullscreen_follows_the_face_without_gameplay():
    video = video_cfg(layout="fullscreen")
    job = make_job(layout=compute_layout(video, 1920, 1080), clip_size=(1920, 1080), segments=[],
                   reframe=[(0.0, 0.75), (2.0, 0.75), (3.0, 0.25)])
    graph = graph_of(build_command(job, full_cfg(video={"layout": "fullscreen"})))
    crop = re.search(r"crop=w=(\d+):h=(\d+):x='([^']+)'", graph)
    crop_w = int(crop.group(1))
    assert (crop_w, int(crop.group(2))) == (608, 1080)
    x = crop.group(3)
    assert evaluate(x, 0) == pytest.approx(0.75 * 1920 - crop_w / 2, abs=0.1)
    assert evaluate(x, 3.5) == pytest.approx(0.25 * 1920 - crop_w / 2, abs=0.1)
    assert evaluate(x, 2.5) == pytest.approx((evaluate(x, 2) + evaluate(x, 3)) / 2, abs=0.1)
    assert "vstack" not in graph and "g.mp4" not in build_command(job, full_cfg())


def test_command_zoom():
    job = make_job(zooms=[Zoom(1.0, 2.0, 0.25, 0.4)], zoom_amount=1.25)
    graph = graph_of(build_command(job, full_cfg()))
    assert "enable='between(t,1.000,2.000)'" in graph
    crop = re.search(r"crop=w=(\d+):h=(\d+):x='([^']+)':y='([^']+)'", graph)
    w, h = int(crop.group(1)), int(crop.group(2))
    assert (w, h) == (864, 486)
    assert evaluate(crop.group(3), 1.5) == pytest.approx(max(0, 0.25 * 1080 - w / 2))
    assert evaluate(crop.group(3), 5) == pytest.approx((1080 - w) / 2)
    assert evaluate(crop.group(4), 1.5) == pytest.approx(min(608 - h, max(0, 0.48 * 608 - h / 2)), abs=0.1)


def test_expressions():
    expr = _piecewise([(1, 2, 10.0), (3, 4, 20.0)], 5.0)
    assert [evaluate(expr, t) for t in (0, 1.5, 2.5, 3.5)] == [5, 10, 5, 20]
    line = _linear([(1, 0.0), (3, 100.0)])
    assert [evaluate(line, t) for t in (0, 1, 2, 3, 9)] == [0, 0, 50, 100, 100]


def test_command_emojis_mute_sfx_and_ducked_music():
    emoji = emoji_path("1f4b0")
    job = make_job(
        emojis=[EmojiShow(emoji, 0.5, 1.2), EmojiShow(emoji, 3.0, 3.6)], emoji_y=500,
        mute=[(1.0, 1.4)], sfx_track=Path("sfx.wav"), music=Path("m.mp3"),
    )
    args = build_command(job, full_cfg())
    graph = graph_of(args)
    assert args.count("-loop") == 1 and str(emoji) in args  # one input per emoji picture
    assert "enable='between(t,0.500,1.200)+between(t,3.000,3.600)'" in graph
    assert "volume=volume=0:enable='between(t,1.000,1.400)'" in graph
    assert "sidechaincompress" in graph and "amix=inputs=3" in graph and "sfx.wav" in args
    job.duck_music = False
    assert "sidechaincompress" not in graph_of(build_command(job, full_cfg()))


def test_encoders():
    v = video_cfg(crf=20, bitrate="10M")
    assert encoder_args("libx265", v)[-2:] == ["-tag:v", "hvc1"]
    nvenc = encoder_args("h264_nvenc", v)
    assert nvenc[nvenc.index("-cq") + 1] == "23" and nvenc[nvenc.index("-bufsize") + 1] == "20M"
    assert "-global_quality" in encoder_args("h264_qsv", v)
    assert "vbr_peak" in encoder_args("h264_amf", v)
    assert encoder_args("h264_videotoolbox", v) == ["-c:v", "h264_videotoolbox", "-b:v", "10M"]
    cfg = full_cfg()
    cfg.video.resolved_codec = "h264_nvenc"
    args = build_command(make_job(), cfg)
    assert args[args.index("-c:v") + 1] == "h264_nvenc"


# ------------------------------------------------------------------ faces (real detector)


def _ffmpeg():
    from brainrot_bot.media import MediaError, find_tools

    try:
        return find_tools()
    except MediaError:
        return None


def test_face_detection_follows_the_person(tmp_path):
    pytest.importorskip("onnxruntime")
    tools = _ffmpeg()
    if tools is None:
        pytest.skip("needs ffmpeg")
    from brainrot_bot.faces import find_faces

    video = tmp_path / "moving.mp4"
    picture = Path(__file__).parent / "data" / "astronaut.jpg"  # public domain NASA photo
    subprocess.run([tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=0x303030:s=640x360:r=10:d=2",
                    "-loop", "1", "-i", str(picture), "-filter_complex", "[0:v][1:v]overlay=x='if(lt(t,1),380,20)':y=60:shortest=1",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(video)], check=True)
    faces = find_faces(tools, video, 0, 2, per_second=4)
    assert len(faces) == 8
    assert all(f.cx > 0.6 for f in faces[:4]) and all(f.cx < 0.4 for f in faces[4:])
    assert all(0.2 < f.cy < 0.45 for f in faces)
    points = track_x(faces, 0, 2, step=0.5)
    assert points[0][1] > 0.6 and points[-1][1] < 0.4
