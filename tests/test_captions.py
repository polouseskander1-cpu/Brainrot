from types import SimpleNamespace

from brainrot_bot.captions import (
    Hook,
    ass_time,
    build_ass,
    build_srt,
    group_words,
    inline_color,
    make_chunks,
    prepare_words,
    style_color,
    time_groups,
)
from brainrot_bot.config import DEFAULTS
from brainrot_bot.transcribe import Word, tidy_words


def caption_cfg(**overrides):
    values = dict(DEFAULTS["captions"])
    values.update(overrides)
    return SimpleNamespace(**values)


def words(*items):
    return [Word(text, start, end) for text, start, end in items]


def test_prepare_strips_punctuation_uppercases_and_marks_pauses():
    prepared = prepare_words(words(("Hello,", 0, 0.4), ("world.", 0.5, 0.9), ("Really?", 1.0, 1.4), ("-", 1.5, 1.6)))
    assert [w.text for w in prepared] == ["HELLO", "WORLD", "REALLY?"]
    assert [w.pause_after for w in prepared] == [True, True, True]


def test_prepare_keeps_inner_apostrophes_and_escapes_ass_codes():
    prepared = prepare_words(words(("don't", 0, 0.3), ("{x}", 0.4, 0.6)), uppercase=False)
    assert [w.text for w in prepared] == ["don't", "x"]


def test_group_words_limits():
    prepared = prepare_words(words(*[(f"w{i}", i * 0.3, i * 0.3 + 0.25) for i in range(7)]))
    groups = group_words(prepared, max_words=3, max_chars=100)
    assert [len(g) for g in groups] == [3, 3, 1]

    long_words = prepare_words(words(("extraordinary", 0, 0.5), ("circumstances", 0.5, 1.0)))
    assert [len(g) for g in group_words(long_words, max_words=3, max_chars=18)] == [1, 1]


def test_group_words_breaks_on_silence_and_sentence_end():
    prepared = prepare_words(words(("one", 0, 0.2), ("two.", 0.3, 0.5), ("three", 0.6, 0.8), ("four", 3.0, 3.2)))
    groups = group_words(prepared, max_words=5, max_chars=100)
    assert [[w.text for w in g] for g in groups] == [["ONE", "TWO"], ["THREE"], ["FOUR"]]


def test_time_groups_never_overlap_and_linger_after_last_word():
    prepared = prepare_words(words(("a", 0.0, 0.2), ("b.", 0.25, 0.4), ("c", 0.5, 0.7), ("d", 5.0, 5.2)))
    chunks = time_groups(group_words(prepared, 3, 100), total=5.3)
    for current, following in zip(chunks, chunks[1:]):
        assert current.end <= following.start
    assert chunks[0].end == 0.5  # held until the next caption starts
    assert abs(chunks[1].end - 1.2) < 1e-9  # 0.5s after its last word, then nothing on screen
    assert chunks[-1].end == 5.3  # clamped to the video length


def test_colors_and_times():
    assert style_color("#FFD400") == "&H0000D4FF"
    assert style_color("#000000", 0.6) == "&H66000000"
    assert inline_color("#00FF66") == "&H66FF00&"
    assert ass_time(0) == "0:00:00.00"
    assert ass_time(3723.456) == "1:02:03.46"


def test_build_ass_has_shadow_and_text_layers():
    cfg = caption_cfg()
    chunks = make_chunks(words(("Hello", 0.0, 0.4), ("there", 0.45, 0.9)), cfg, total=2.0)
    text = build_ass(chunks, width=1080, height=1920, duration=2.0, captions=cfg)
    assert "PlayResX: 1080" in text and "PlayResY: 1920" in text
    assert "Style: Caption,Montserrat Black,100,&H0000D4FF" in text
    dialogue = [line for line in text.splitlines() if line.startswith("Dialogue:")]
    assert len(dialogue) == 2
    assert dialogue[0].startswith("Dialogue: 0,") and "\\1a&HFF&" in dialogue[0] and "\\blur7" in dialogue[0]
    assert dialogue[1].startswith("Dialogue: 1,") and dialogue[1].endswith("HELLO THERE")
    assert "\\pos(540,960)" in dialogue[1] and "\\fscx70" in dialogue[1]


def test_build_ass_highlight_and_hook():
    cfg = caption_cfg(highlight_color="#00FF66", animation="none", shadow_opacity=0)
    chunks = make_chunks(words(("Hi", 0.0, 0.3), ("you", 0.4, 0.8)), cfg, total=3.0)
    hook_cfg = SimpleNamespace(**DEFAULTS["hook"])
    text = build_ass(chunks, width=1080, height=1920, duration=3.0, captions=cfg, hook_cfg=hook_cfg, hook=Hook("Title\nPART 1/2", 675, 4))
    dialogue = [line for line in text.splitlines() if line.startswith("Dialogue:")]
    assert len(dialogue) == 2  # no shadow layer, one caption, one hook
    assert "\\t(0,1,\\1c&H66FF00&)" in dialogue[0] and "\\t(400,401,\\1c&H0000D4FF&)" not in dialogue[0]
    assert "\\t(400,401,\\1c&H66FF00&)" in dialogue[0]
    assert "\\fscx70" not in dialogue[0]
    hook_line = dialogue[1]
    assert ",Hook," in hook_line and hook_line.endswith("Title\\NPART 1/2")
    assert hook_line.startswith("Dialogue: 2,0:00:00.00,0:00:03.00")  # hook clamped to the video length


def test_build_srt():
    srt = build_srt(words(("Hello", 0.0, 0.5), ("world.", 0.6, 1.0), ("Bye", 3.0, 3.4)))
    assert srt.splitlines()[:3] == ["1", "00:00:00,000 --> 00:00:01,600", "Hello world."]
    assert "00:00:03,000 --> 00:00:04,000" in srt


def test_tidy_words_fixes_bad_timings():
    fixed = tidy_words(words(("b", 1.0, 0.9), ("a", 0.5, 1.2), ("", 2, 3), ("c", -1, 0.1)))
    assert [w.text for w in fixed] == ["c", "a", "b"]
    for w in fixed:
        assert w.end > w.start >= 0
    for current, following in zip(fixed, fixed[1:]):
        assert current.start <= following.start
