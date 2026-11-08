"""Shot editor: slip, length, speed, framing, order, replace, insert, delete, fades, persistence, render."""
import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from kartcut import autocut, beats, editing, render, server
from kartcut import project as P


@pytest.fixture()
def edit_setup(analysed, shoot, tmp_path):
    dst = tmp_path / "edit"
    shutil.copytree(analysed.root, dst)
    prj = P.Project(dst)
    prj.finalise()
    music = beats.analyse_music(shoot["music"], 0.0, prj.cache)
    d23 = next(d["id"] for d in prj.data["drivers"].values() if d["number"] == "23")
    tl = autocut.plan(prj, "driver", d23, "9:16", 10.0, music)
    return prj, tl


def _check(prj, tl):
    assert tl.frames == sum(c.frames for c in tl.clips)
    start = 0
    for c in tl.clips:
        assert c.start_f == start
        start += c.frames
        dur = prj.data["clips"][c.clip_id]["duration"]
        assert 0.0 < c.src_in and c.src_in + c.src_dur <= dur - 0.04
        assert c.src_dur == pytest.approx(c.frames / tl.fps * c.speed, abs=1e-3)


def test_edit_operations(edit_setup):
    prj, tl = edit_setup
    assert len(tl.clips) >= 2
    first, second = tl.clips[0], tl.clips[1]
    s2 = second.start_f
    bf = editing.beat_frames(tl)
    editing.apply(prj, tl, "beats", {"shot": 0, "by": 1})          # lengthen by a beat: everything after shifts
    assert tl.clips[1].start_f == s2 + bf or tl.clips[0].frames < first.frames + bf  # (unless the clip ran out)
    _check(prj, tl)
    editing.apply(prj, tl, "slip", {"shot": 0, "src_in": 999})       # past the end: clamped inside the clip
    _check(prj, tl)
    editing.apply(prj, tl, "nudge", {"shot": 0, "by": -0.5})
    _check(prj, tl)
    editing.apply(prj, tl, "seconds", {"shot": 1, "seconds": 0.8})
    assert tl.clips[1].frames == round(0.8 * tl.fps)
    editing.apply(prj, tl, "speed", {"shot": 1, "speed": 0.5})
    assert tl.clips[1].src_dur == pytest.approx(tl.clips[1].frames / tl.fps * 0.5, abs=1e-3)
    _check(prj, tl)
    editing.apply(prj, tl, "crop", {"shot": 0, "x": 0.3})
    if tl.clips[0].orientation != "vertical":
        assert len(tl.clips[0].crop) == 1
    editing.apply(prj, tl, "crop", {"shot": 0, "x": None})
    ids = [c.clip_id for c in tl.clips]
    editing.apply(prj, tl, "move", {"shot": 0, "to": 1})
    assert [c.clip_id for c in tl.clips][:2] == [ids[1], ids[0]]
    alts = editing.alternatives(prj, tl)
    assert alts
    alt = next(a for a in alts if not a["in_edit"]) if any(not a["in_edit"] for a in alts) else alts[0]
    n = len(tl.clips)
    editing.apply(prj, tl, "insert", {"after": 0, "clip": alt["clip"], "peak": alt["peak"]})
    assert len(tl.clips) == n + 1 and tl.clips[1].clip_id == alt["clip"]
    editing.apply(prj, tl, "replace", {"shot": 0, "clip": alt["clip"], "peak": alt["peak"]})
    assert tl.clips[0].clip_id == alt["clip"]
    editing.apply(prj, tl, "delete", {"shot": 1})
    assert len(tl.clips) == n
    editing.apply(prj, tl, "fades", {"fade_in": True, "fade_out": False})
    assert tl.fade_in and not tl.fade_out
    _check(prj, tl)
    with pytest.raises(ValueError):
        editing.apply(prj, tl, "slip", {"shot": 99, "src_in": 1})
    # round trip through the saved form
    tl2 = autocut.Timeline.from_dict(json.loads(json.dumps(tl.to_dict())))
    assert [c.clip_id for c in tl2.clips] == [c.clip_id for c in tl.clips] and tl2.fade_in
    # and it still renders to exactly the edited length
    out = render.render_timeline(prj, tl2, prj.root / "renders" / "edited.mp4")
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json",
                                       str(out)], capture_output=True, text=True).stdout)
    assert float(probe["format"]["duration"]) == pytest.approx(tl2.duration, abs=0.15)


def test_editor_api_and_persistence(analysed, shoot, tmp_path):
    dst = tmp_path / "api"
    shutil.copytree(analysed.root, dst)
    prj = P.Project(dst)
    prj.settings["music_path"] = str(shoot["music"])
    prj.finalise()
    server.S.project = prj
    server.S.plans, server.S.music = [], None
    cl = TestClient(server.app)
    r = cl.post("/api/montage/plan", json={"event": True, "reels": False, "aspects": ["16:9"], "event_length": 10})
    assert r.status_code == 200
    plan0 = r.json()["plans"][0]
    assert plan0["beat_frames"] > 0 and "proxy" in plan0["clips"][0]
    r = cl.post("/api/montage/edit", json={"plan": 0, "op": "beats", "shot": 0, "by": 1})
    assert r.status_code == 200
    edited = r.json()
    server.S.plans = []                                   # e.g. KartCut restarted
    again = cl.get("/api/montage/plans").json()["plans"][0]
    assert again["frames"] == edited["frames"]
    assert cl.get("/api/montage/alternatives", params={"plan": 0}).json()
    assert cl.get("/api/music/file").status_code == 200
    assert cl.post("/api/montage/edit", json={"plan": 0, "op": "bogus"}).status_code == 400
