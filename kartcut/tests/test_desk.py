"""Kart sorting desk: bulk assign, not-a-kart, whole-frame view, lookalike groups."""
import shutil

from conftest import by_name
from fastapi.testclient import TestClient

from kartcut import project as P
from kartcut import server


def _client(analysed, tmp_path):
    dst = tmp_path / "desk"
    shutil.copytree(analysed.root, dst)
    prj = P.Project(dst)
    prj.finalise()
    server.S.project = prj
    return prj, TestClient(server.app)


def test_desk_assign_notkart_and_context(analysed, tmp_path):
    prj, cl = _client(analysed, tmp_path)
    cands = cl.get("/api/drivers/candidates").json()
    assert cands and all({"group", "group_size", "orientation", "notkart", "source"} <= set(c) for c in cands)
    # lookalikes come out next to each other: the five #23 karts share one group
    c1 = by_name(prj, "C0001.MP4")
    ids = {d["number"]: d["id"] for d in prj.data["drivers"].values()}

    # a wrong detection: mark the #7 kart in C0001 as "not a kart" -> it leaves everything
    t7 = next(c for c in cands if c["clip"] == c1 and c["driver"] == ids["7"])
    r = cl.post("/api/karts/assign", json={"tracks": [[c1, t7["track"]]], "driver": "notkart"})
    assert r.status_code == 200
    ident = prj.derived["clips"][c1]["identity"]
    assert ids["7"] not in [d["id"] for d in ident["drivers"]]
    assert all(str(t7["track"]) not in [str(x) for x in s["tracks"]] for s in prj.derived["segments"] if s["clip"] == c1)
    again = {(c["clip"], c["track"]): c for c in cl.get("/api/drivers/candidates").json()}
    assert again[(c1, t7["track"])]["notkart"]
    detail = cl.get(f"/api/clips/{c1}").json()
    assert any(t["notkart"] for t in detail["tracks"])

    # back to automatic restores it
    cl.post("/api/karts/assign", json={"tracks": [[c1, t7["track"]]], "driver": None})
    assert ids["7"] in [d["id"] for d in prj.derived["clips"][c1]["identity"]["drivers"]]

    # other racer
    cl.post("/api/karts/assign", json={"tracks": [[c1, t7["track"]]], "driver": "none"})
    again = {(c["clip"], c["track"]): c for c in cl.get("/api/drivers/candidates").json()}
    assert again[(c1, t7["track"])]["other"] and again[(c1, t7["track"])]["source"] == "manual"

    # whole-frame view
    r = cl.get("/api/karts/context", params={"clip": c1, "track": t7["track"]})
    assert r.status_code == 200
    assert (prj.root / r.json()["url"]).exists()
    assert cl.get(f"/media/{r.json()['url']}").status_code == 200

    assert cl.post("/api/karts/assign", json={"tracks": [[c1, 1]], "driver": "nobody"}).status_code == 400


def test_lookalike_groups_cover_all_karts(analysed, tmp_path):
    prj, cl = _client(analysed, tmp_path)
    cands = cl.get("/api/drivers/candidates").json()
    by_drv = {}
    for c in cands:
        by_drv.setdefault(c["driver"], set()).add(c["group"])
    ids = {d["number"]: d["id"] for d in prj.data["drivers"].values()}
    # most #23 karts land in one lookalike group, sorted first (biggest group)
    g23 = [c["group"] for c in cands if c["driver"] == ids["23"]]
    top = max(set(g23), key=g23.count)
    assert g23.count(top) >= 3
    assert cands[0]["group_size"] >= cands[-1]["group_size"]
