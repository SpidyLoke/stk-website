import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from conftest import by_name


def probe_dims(p):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
                          "stream=width,height:format=duration", "-of", "json", str(p)],
                         capture_output=True, text=True).stdout
    j = json.loads(out)
    s = j["streams"][0]
    return s["width"], s["height"], float(j["format"]["duration"])


def test_profiles_and_orientation(analysed):
    prj = analysed
    an = prj.load_analysis(by_name(prj, "C0001.MP4"))
    assert an["profile"] == "slog3-sgamut3cine" and an["profile_source"] == "camera metadata"
    phone = prj.load_analysis(by_name(prj, "PHONE_0009.MP4"))
    assert phone["profile"] == "rec709"
    vert = prj.load_analysis(by_name(prj, "C0008.MP4"))
    assert vert["orientation"] == "vertical"
    assert prj.load_analysis(by_name(prj, "C0012.MP4"))["probe"]["fps"] == pytest.approx(50, abs=0.1)


def test_exposure_and_wb(analysed):
    clips = analysed.derived["clips"]
    under = clips[by_name(analysed, "C0003.MP4")]["exposure"]
    assert under["level"] == "bad"
    assert under["ev_rel"] == pytest.approx(-2.0, abs=0.25)   # rendered exactly 2 stops under
    assert clips[by_name(analysed, "C0001.MP4")]["exposure"]["level"] == "good"
    warm = clips[by_name(analysed, "C0004.MP4")]["exposure"]
    assert any("Warmer" in i for i in warm["issues"]), warm


def test_junk_filter(analysed):
    clips = analysed.derived["clips"]
    assert any("Black" in r for r in clips[by_name(analysed, "C0005.MP4")]["junk"]["reasons"])
    assert any("Too short" in r for r in clips[by_name(analysed, "C0006.MP4")]["junk"]["reasons"])
    assert any("Duplicate of C0003" in r for r in clips[by_name(analysed, "C0007.MP4")]["junk"]["reasons"])
    # same-tripod lookalikes must NOT be duplicates
    assert not clips[by_name(analysed, "C0012.MP4")]["junk"]["is_junk"]
    assert not clips[by_name(analysed, "C0001.MP4")]["junk"]["is_junk"]
    assert not clips[by_name(analysed, "C0010.MP4")]["junk"]["is_junk"]   # b-roll is not junk


def test_quality_and_highlights(analysed):
    clips = analysed.derived["clips"]
    assert {"battle", "overtake"} <= set(clips[by_name(analysed, "C0001.MP4")]["tags"])
    assert "sharp pan" in clips[by_name(analysed, "C0002.MP4")]["tags"]
    assert "shaky" in clips[by_name(analysed, "C0004.MP4")]["tags"]
    assert "soft focus" in clips[by_name(analysed, "C0011.MP4")]["tags"]
    segs = [s for s in analysed.derived["segments"] if s["clip"] == by_name(analysed, "C0001.MP4")]
    ov = [s for s in segs if s["type"] == "overtake"]
    assert ov and "Overtake: #23 Ahmad Zaki on #7 Lee Wei" == ov[0]["label"]


def test_driver_identification(analysed):
    prj = analysed
    d = {x["number"]: x["id"] for x in prj.data["drivers"].values()}
    ident = lambda n: prj.derived["clips"][by_name(prj, n)]["identity"]  # noqa: E731
    assert {x["id"] for x in ident("C0001.MP4")["drivers"]} == {d["23"], d["7"]}
    assert ident("C0002.MP4")["main"] == d["23"]
    assert ident("C0004.MP4")["main"] == d["12"]
    # under-exposed #7: OCR after brightness normalisation, or colour match once a reference is set
    if ident("C0003.MP4")["main"] != d["7"]:
        c1 = prj.load_analysis(by_name(prj, "C0001.MP4"))
        t7 = next(t for t in c1["tracks"] if any(o[0] == "7" for o in t["ocr"]))
        prj.set_reference(d["7"], by_name(prj, "C0001.MP4"), t7["id"])
        prj.finalise()
    assert ident("C0003.MP4")["main"] == d["7"]


def test_manual_correction_learns(analysed):
    prj = analysed
    d = {x["number"]: x["id"] for x in prj.data["drivers"].values()}
    cid = by_name(prj, "C0011.MP4")
    tid = prj.load_analysis(cid)["tracks"][0]["id"]
    prj.assign_track(cid, tid, d["12"])
    prj.finalise()
    assert prj.derived["clips"][cid]["identity"]["main"] == d["12"]
    prj.assign_track(cid, tid, None)
    prj.finalise()
    assert prj.derived["clips"][cid]["identity"]["main"] == d["23"]


def test_shot_match(analysed):
    from kartcut import shotmatch

    prj = analysed
    prj.data["hero"] = by_name(prj, "C0001.MP4")
    res = shotmatch.run_shot_match(prj)
    corr = prj.data["shot_match"]["corrections"]
    assert corr[by_name(prj, "C0003.MP4")]["ev"] == pytest.approx(2.0, abs=0.3)
    g = corr[by_name(prj, "C0004.MP4")]["gains"]
    assert g[0] < 0.92 and g[2] > 1.08          # pulls the warm clip back towards neutral
    assert Path(res["report"]).exists()
    assert len(list((Path(res["folder"]) / "luts").glob("*.cube"))) >= 10


def test_plan_render_export(analysed, shoot):
    from kartcut import autocut, beats, export_nle, render

    prj = analysed
    music = beats.analyse_music(shoot["music"], 0.0, prj.cache)
    assert music["tempo"] == pytest.approx(120, abs=3)
    assert music["drop"] == pytest.approx(12.0, abs=1.5)
    ev = autocut.plan(prj, "event", None, "16:9", 14.0, music, seed=1)
    junk = {cid for cid, i in prj.derived["clips"].items() if i["junk"]["is_junk"]}
    assert ev.clips and not any(c.clip_id in junk for c in ev.clips)
    beat_frames = {round(b * ev.fps) for b in [0.0] + music["beats"]}
    assert all(c.start_f in beat_frames for c in ev.clips)
    assert sum(c.frames for c in ev.clips) == ev.frames
    d23 = next(x["id"] for x in prj.data["drivers"].values() if x["number"] == "23")
    reel = autocut.plan(prj, "driver", d23, "9:16", 8.0, music, seed=1)
    assert reel.clips and all(d23 in c.drivers for c in reel.clips)
    assert any(c.crop for c in reel.clips if c.orientation == "horizontal")

    out1 = render.render_timeline(prj, ev, prj.root / "renders" / "event.mp4")
    w, h, dur = probe_dims(out1)
    assert (w, h) == (1920, 1080) and dur == pytest.approx(ev.duration, abs=0.15)
    out2 = render.render_timeline(prj, reel, prj.root / "renders" / "reel.mp4")
    w, h, dur = probe_dims(out2)
    assert (w, h) == (1080, 1920) and dur == pytest.approx(reel.duration, abs=0.15)

    ex = export_nle.export_all(prj, [ev, reel])
    tree = ET.parse(ex["xml"])
    assert tree.getroot().tag == "xmeml"
    assert len(tree.findall(".//sequence")) == 2
    assert tree.findall(".//bin/name")
    _run_resolve_script(Path(ex["lua"]), len(ev.clips) + len(reel.clips))


def _run_resolve_script(path: Path, n_timeline_clips: int):
    lupa = pytest.importorskip("lupa")
    L = lupa.LuaRuntime(unpack_returned_tuples=True)
    log = {"imports": 0, "markers": 0, "folders": 0, "timelines": 0, "appended": 0, "luts": 0}

    class Item:
        def AddMarker(self, *a):
            log["markers"] += 1
            return True

        def SetClipColor(self, c):
            return True

        def GetClipProperty(self, k):
            return "48000"

        def SetLUT(self, n, p):
            log["luts"] += 1
            return True

    class Timeline:
        def AddTrack(self, *a):
            return True

        def GetStartFrame(self):
            return 86400

        def AddMarker(self, *a):
            return True

    class MP:
        def GetRootFolder(self):
            return object()

        def AddSubFolder(self, parent, name):
            log["folders"] += 1
            return object()

        def SetCurrentFolder(self, f):
            return True

        def ImportMedia(self, paths):
            log["imports"] += 1
            return L.table_from([Item()])

        def CreateEmptyTimeline(self, name):
            log["timelines"] += 1
            return Timeline()

        def AppendToTimeline(self, infos):
            n = len(list(infos.values())) if hasattr(infos, "values") else 0
            log["appended"] += n
            return L.table_from([Item() for _ in range(n)])

    class Project:
        def GetMediaPool(self):
            return mp

        def SetCurrentTimeline(self, tl):
            return True

    class PM:
        def GetCurrentProject(self):
            return Project()

    class Resolve:
        def GetProjectManager(self):
            return PM()

    mp = MP()
    L.globals()["resolve"] = Resolve()
    src = path.read_text(encoding="utf-8")
    # lupa exposes Python methods with ':' calls fine; run the script as Resolve would
    L.execute(src)
    assert log["timelines"] == 2
    assert log["imports"] > 0 and log["markers"] > 0
    assert log["appended"] >= n_timeline_clips


def test_offload_and_verify(shoot, tmp_path):
    from kartcut import offload

    card = tmp_path / "card"
    (card / "PRIVATE" / "M4ROOT" / "CLIP").mkdir(parents=True)
    for n in ("C0001.MP4", "C0001M01.XML", "C0002.MP4"):
        (card / "PRIVATE" / "M4ROOT" / "CLIP" / n).write_bytes((shoot["dir"] / n).read_bytes())
    a, b = tmp_path / "ssdA", tmp_path / "ssdB"
    res = offload.offload([{"path": str(card), "reel": "CamA_Card1"}], [str(a), str(b)], "Sepang")
    assert res["verified"] and res["safe_to_format"] and res["files"] == 3
    copy = a / "Sepang" / "CamA_Card1" / "PRIVATE" / "M4ROOT" / "CLIP" / "C0002.MP4"
    assert copy.read_bytes() == (shoot["dir"] / "C0002.MP4").read_bytes()
    # corrupt one copy (same size) -> re-run must catch it
    data = bytearray(copy.read_bytes())
    data[5000] ^= 0xFF
    copy.write_bytes(bytes(data))
    res2 = offload.offload([{"path": str(card), "reel": "CamA_Card1"}], [str(a), str(b)], "Sepang")
    assert not res2["verified"] and not res2["safe_to_format"] and len(res2["mismatches"]) == 1


def test_sort_on_disk(analysed, tmp_path):
    from kartcut import sortdisk

    res = sortdisk.sort_on_disk(analysed, tmp_path / "Sorted", "link")
    assert res["placed"] > 0
    assert (tmp_path / "Sorted" / "Senior" / "#23 Ahmad Zaki" / "C0002.MP4").exists()
    assert (tmp_path / "Sorted" / "Senior" / "#23 Ahmad Zaki" / "C0002M01.XML").exists()
    assert (tmp_path / "Sorted" / "Junior" / "#7 Lee Wei" / "C0001.MP4").exists()
    assert (tmp_path / "Sorted" / "_B-roll" / "C0010.MP4").exists()
    assert not list((tmp_path / "Sorted").rglob("C0005.MP4"))  # junk stays out


def test_api_smoke(analysed):
    from fastapi.testclient import TestClient

    from kartcut import server

    server.S.project = analysed
    c = TestClient(server.app)
    st = c.get("/api/status").json()
    assert st["project"]["name"] == "Test Race"
    clips = c.get("/api/clips").json()
    assert clips["bins"]["categories"]["Senior"][0]["count"] >= 3
    cid = by_name(analysed, "C0001.MP4")
    det = c.get(f"/api/clips/{cid}").json()
    assert len(det["tracks"]) == 2 and det["segments"]
    assert c.get("/").status_code == 200
    thumb = det["summary"]["thumb"]
    assert c.get(f"/media/{thumb}").status_code == 200
    assert c.get("/media/../../etc/passwd").status_code == 404
    r = c.post("/api/montage/plan", json={"event": True, "reels": False, "aspects": ["16:9"], "event_length": 10})
    assert r.status_code == 200 and r.json()["plans"][0]["clips"]
    ex = c.get("/api/exposure").json()
    assert ex["rows"]
