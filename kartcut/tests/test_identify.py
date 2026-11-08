"""Driver ID from appearance alone (numbers unreadable), other racers, and kart groups."""
import shutil

from conftest import by_name

from kartcut import project as P


def _copy(analysed, tmp_path):
    dst = tmp_path / "copy"
    shutil.copytree(analysed.root, dst)
    return P.Project(dst)


def test_appearance_only_matching(analysed, tmp_path):
    prj = _copy(analysed, tmp_path)
    ids = {d["number"]: d["id"] for d in prj.data["drivers"].values()}
    # Forget the numbers: KartCut must go on helmet / suit / kart colours only.
    prj.data["drivers"] = {ids["23"]: {**prj.data["drivers"][ids["23"]], "number": "", "refs": []},
                           ids["7"]: {**prj.data["drivers"][ids["7"]], "number": "", "refs": []}}
    prj.data["track_overrides"] = {}
    for t in (t for an in prj.all_analyses().values() for t in an["tracks"]):
        t["ocr"] = []
    c2 = by_name(prj, "C0002.MP4")
    c1 = by_name(prj, "C0001.MP4")
    t23 = prj.load_analysis(c2)["tracks"][0]["id"]
    an1 = prj.load_analysis(c1)
    # in C0001 the #7 kart is the higher one on screen (smaller y)
    t7 = min(an1["tracks"], key=lambda t: t["frames"][0][2])["id"]
    prj.set_reference(ids["23"], c2, t23)
    prj.set_reference(ids["7"], c1, t7)
    d = prj.finalise()
    main = lambda n: d["clips"][by_name(prj, n)]["identity"]["main"]  # noqa: E731
    assert main("C0011.MP4") == ids["23"]
    assert main("C0012.MP4") == ids["23"]
    assert main("PHONE_0009.MP4") == ids["23"]
    assert main("C0003.MP4") == ids["7"]
    assert main("C0004.MP4") is None          # green #12 isn't on this entry list
    assert d["id_stats"]["negatives"] >= 1     # threshold learnt from karts sharing a frame


def test_other_racer_and_groups(analysed, tmp_path):
    prj = _copy(analysed, tmp_path)
    ids = {d["number"]: d["id"] for d in prj.data["drivers"].values()}
    prj.data["drivers"] = {ids["7"]: {**prj.data["drivers"][ids["7"]], "number": "", "refs": []}}
    prj.data["track_overrides"] = {}
    for t in (t for an in prj.all_analyses().values() for t in an["tracks"]):
        t["ocr"] = []
    d = prj.finalise()
    groups = d["groups"]
    # all the #23 karts (5+ clips) form one nameable group
    big = groups[0]
    clips = {cid for cid, _ in big["tracks"]}
    assert {by_name(prj, n) for n in ("C0002.MP4", "C0011.MP4", "C0012.MP4")} <= clips
    # name the whole group "other racer" in one go: they leave Needs review
    for cid, tid in big["tracks"]:
        prj.data["track_overrides"].setdefault(cid, {})[str(tid)] = "none"
    d = prj.finalise()
    for n in ("C0002.MP4", "C0011.MP4", "C0012.MP4"):
        assert not d["clips"][by_name(prj, n)]["identity"]["needs_review"]
