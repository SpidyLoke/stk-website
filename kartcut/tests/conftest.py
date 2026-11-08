import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import synth  # noqa: E402


@pytest.fixture(scope="session")
def shoot(tmp_path_factory):
    """Synthetic shoot, generated once per test session (or reused via KARTCUT_TEST_SHOOT)."""
    reuse = os.environ.get("KARTCUT_TEST_SHOOT")
    if reuse and (Path(reuse) / "ground_truth.json").exists():
        d = Path(reuse)
    else:
        d = tmp_path_factory.mktemp("shoot")
        synth.make_shoot(d)
    music = d / "music.wav"
    if not music.exists():
        synth.make_music(music)
    gt = json.loads((d / "ground_truth.json").read_text())
    return {"dir": d, "gt": gt, "music": music}


@pytest.fixture(scope="session")
def analysed(shoot, tmp_path_factory):
    from kartcut import analyse, detect
    from kartcut import project as P

    root = tmp_path_factory.mktemp("proj")
    prj = P.Project(root / "Test Race", "Test Race")
    prj.add_folder(shoot["dir"])
    m = detect.Models(prj.settings)
    m._detector = synth.SynthDetector(shoot["gt"])
    m._pose = False
    m._embedder = False
    for cid, c in sorted(prj.data["clips"].items(), key=lambda kv: kv[1]["name"]):
        prj.save_analysis(cid, analyse.analyse_clip(c, prj.root, prj.settings, m))
    from kartcut import fingerprint

    fingerprint.refresh(prj, m)
    prj.import_entry_list("No\tName\tClass\n23\tAhmad Zaki\tSenior\n7\tLee Wei\tJunior\n12\tPriya Nair\tJunior\n")
    prj.finalise()
    return prj


def by_name(prj, name):
    return next(cid for cid, c in prj.data["clips"].items() if c["name"] == name)
