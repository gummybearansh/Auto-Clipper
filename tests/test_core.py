from clipper.captions import build_events
from clipper.config import CaptionCfg
from clipper.jobs import plan_chunks
from clipper.pipeline import clips_for
from clipper.signals.chat import classify, dominant_reaction
from clipper.titles import shouted_phrase


def test_chunks_never_exceed_30_min():
    for dur in (100, 1799, 1800, 1801, 3700, 9711):
        for live, follow in ((False, False), (True, False), (True, True)):
            ch = plan_chunks(dur, live, follow)
            assert all(b - a <= 1800 for a, b in ch)
            assert all(b <= dur for a, b in ch)


def test_chunk_plan_live():
    # 2h42m live stream, snapshot: 5 full blocks + the tail (minus safety buffer)
    ch = plan_chunks(9711, live=True, follow=False)
    assert len(ch) == 6 and ch[-1][1] == 9711 - 90
    # following: only full blocks; tail waits for more footage / stream end
    assert len(plan_chunks(9711, live=True, follow=True)) == 5
    assert plan_chunks(3600, live=False, follow=True) == [(0, 1800), (1800, 3600)]


def test_short_video_is_one_chunk():
    assert plan_chunks(30.4, live=False, follow=False) == [(0.0, 30.4)]
    assert plan_chunks(3, live=False, follow=False) == []


def test_clips_per_chunk():
    assert clips_for(1800, 10) == 10
    assert clips_for(900, 10) == 5
    assert clips_for(1536, 10) == 9
    assert clips_for(30, 10) == 1


def _w(text, s, step=0.3, gap_after=None):
    out = []
    for i, t in enumerate(text.split()):
        out.append({"w": t, "s": s + i * step, "e": s + i * step + step * 0.9, "p": 0.9})
    return out


def test_caption_groups_max_three_words_and_no_overlap():
    words = _w("oh my god no way he actually hit that", 10.0)
    ev = build_events(words, 9.0, 20.0, CaptionCfg())
    assert ev and all(len(e.words) <= 3 for e in ev)
    assert all(a.end <= b.start + 1e-6 for a, b in zip(ev, ev[1:]))
    assert ev[0].start >= 1.0 - 1e-6           # clip-relative
    assert all(e.words[e.hi].isupper() for e in ev)


def test_no_captions_without_speech():
    assert build_events([], 0, 30, CaptionCfg()) == []


def test_chat_reaction():
    assert "laugh" in classify("KEKW KEKW")
    assert "clip" in classify("CLIP IT")
    chat = [(10 + i * 0.2, "KEKW") for i in range(30)] + [(12, "hello")]
    cat, _ = dominant_reaction(chat, 0, 30, 0)
    assert cat == "laugh"


def test_shouted_phrase_rejects_fragments():
    import numpy as np
    loud = np.zeros(60)
    loud[20:23] = 15
    words = _w("i can't wait to hate on my", 5) + _w("no way he did that", 20.0, step=0.4)
    words[-1]["w"] = "that!"
    t = shouted_phrase(words, loud)
    assert t is None or not t.endswith(" MY")
