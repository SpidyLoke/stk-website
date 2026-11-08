"""Moving clips between drivers, re-sorting folders after a move, sharper kart previews."""
import shutil

import cv2
from conftest import by_name
from fastapi.testclient import TestClient

from kartcut import fingerprint, server, sortdisk
from kartcut import project as P


def _copy(analysed, tmp_path):
    dst = tmp_path / "copy"
    shutil.copytree(analysed.root, dst)
    prj = P.Project(dst)
    prj.finalise()
    return prj


def test_move_clip_and_resort_folders(analysed, tmp_path):
    prj = _copy(analysed, tmp_path)
    ids = {d["number"]: d["id"] for d in prj.data["drivers"].values()}
    c2 = by_name(prj, "C0002.MP4")
    sorted_dir = tmp_path / "Sorted"
    sortdisk.sort_on_disk(prj, sorted_dir, "link")
    old = sorted_dir / "Senior" / "#23 Ahmad Zaki" / "C0002.MP4"
    assert old.exists()

    server.S.project = prj
    cl = TestClient(server.app)
    assert cl.post("/api/clips/move", json={"clips": [c2], "driver": ids["7"]}).status_code == 200
    ident = prj.derived["clips"][c2]["identity"]
    assert [d["id"] for d in ident["drivers"]] == [ids["7"]]
    main_tid = str(prj.load_analysis(c2)["tracks"][0]["id"])
    assert prj.data["track_overrides"][c2][main_tid] == ids["7"]   # the matcher learns from the move

    res = sortdisk.sort_on_disk(prj, sorted_dir, "link")
    assert res["moved_out"] >= 1
    assert not old.exists()
    assert (sorted_dir / "Junior" / "#7 Lee Wei" / "C0002.MP4").exists()
    assert (analysed.root.parent).exists()
    src = prj.data["clips"][c2]["path"]
    assert P.Path(src).exists()                                       # original footage untouched

    cl.post("/api/clips/move", json={"clips": [c2], "driver": ids["23"], "mode": "add"})
    assert {d["id"] for d in prj.derived["clips"][c2]["identity"]["drivers"]} == {ids["7"], ids["23"]}
    cl.post("/api/clips/move", json={"clips": [c2], "driver": ids["7"], "mode": "remove"})
    assert [d["id"] for d in prj.derived["clips"][c2]["identity"]["drivers"]] == [ids["23"]]
    cl.post("/api/clips/move", json={"clips": [c2], "driver": None})
    assert prj.derived["clips"][c2]["identity"]["drivers"] == []


def test_sort_never_removes_files_it_did_not_place(analysed, tmp_path):
    prj = _copy(analysed, tmp_path)
    sorted_dir = tmp_path / "Sorted2"
    mine = sorted_dir / "Senior" / "#23 Ahmad Zaki" / "C0002.MP4"
    mine.parent.mkdir(parents=True)
    mine.write_bytes(b"my own file")
    sortdisk.sort_on_disk(prj, sorted_dir, "link")
    c2 = by_name(prj, "C0002.MP4")
    ids = {d["number"]: d["id"] for d in prj.data["drivers"].values()}
    with prj.lock:
        prj.data["clip_overrides"][c2] = [ids["7"]]
    prj.finalise()
    sortdisk.sort_on_disk(prj, sorted_dir, "link")
    assert mine.read_bytes() == b"my own file"


def test_hd_kart_previews(analysed, tmp_path):
    prj = _copy(analysed, tmp_path)
    n = fingerprint.hd_crops(prj)
    assert n > 0
    an = prj.load_analysis(by_name(prj, "C0001.MP4"))
    for tr in an["tracks"]:
        if tr.get("crop"):
            assert tr.get("crop_hd")
            assert cv2.imread(str(prj.root / tr["crop"])) is not None
    assert fingerprint.refresh(prj) > 0      # fingerprints redone from the new previews
    assert fingerprint.hd_crops(prj) == 0    # nothing left to redo
