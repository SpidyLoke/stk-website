"""Hand the edit to Resolve / Premiere.

* Premiere (and Resolve fallback): FCP7 XML with driver bins, highlight markers and
  every planned timeline.
* DaVinci Resolve: a Lua script for Workspace > Scripts that builds driver bins,
  clip colours, markers, applies the shot-match LUT per clip and builds the
  timelines natively. Lua ships inside Resolve (free and Studio), nothing to install.
"""

from __future__ import annotations

import math
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote

from .autocut import Timeline
from .util import safe_name

MARKER_COLOUR = {"overtake": "Red", "battle": "Yellow", "incident": "Purple", "podium": "Green",
                 "celebration": "Green", "grid": "Cyan", "pan": "Blue", "action": "Sky", "broll": "Cream"}
SECTION_CLIP_COLOUR = {"intro": "Teal", "build": "Blue", "climax": "Orange", "outro": "Green"}


def _rate(fps: float) -> tuple[int, bool]:
    for nominal in (24, 30, 60, 120):
        if abs(fps - nominal * 1000 / 1001) < 0.02:
            return nominal, True
    return int(round(fps)), False


def _rate_el(parent, fps: float):
    tb, ntsc = _rate(fps)
    r = ET.SubElement(parent, "rate")
    ET.SubElement(r, "timebase").text = str(tb)
    ET.SubElement(r, "ntsc").text = "TRUE" if ntsc else "FALSE"
    return r


def pathurl(p: str) -> str:
    s = str(p).replace("\\", "/")
    if not s.startswith("/"):
        s = "/" + s
    return "file://localhost" + quote(s, safe="/")


class _Files:
    def __init__(self, project):
        self.project = project
        self.ids: dict[str, str] = {}

    def el(self, parent, cid: str):
        """Full <file> the first time, a reference after that (as FCP7 XML expects)."""
        if cid in self.ids:
            ET.SubElement(parent, "file", id=self.ids[cid])
            return
        fid = f"file-{len(self.ids) + 1}"
        self.ids[cid] = fid
        c = self.project.data["clips"][cid]
        an = self.project.load_analysis(cid) or {}
        pr = an.get("probe", {})
        fps = float(c.get("fps") or 25)
        f = ET.SubElement(parent, "file", id=fid)
        ET.SubElement(f, "name").text = c["name"]
        ET.SubElement(f, "pathurl").text = pathurl(c["path"])
        _rate_el(f, fps)
        ET.SubElement(f, "duration").text = str(int(round(float(c.get("duration") or 0) * fps)))
        m = ET.SubElement(f, "media")
        v = ET.SubElement(m, "video")
        sc = ET.SubElement(v, "samplecharacteristics")
        _rate_el(sc, fps)
        ET.SubElement(sc, "width").text = str(pr.get("display_w") or c.get("width") or 1920)
        ET.SubElement(sc, "height").text = str(pr.get("display_h") or c.get("height") or 1080)
        ET.SubElement(sc, "pixelaspectratio").text = "square"
        if pr.get("has_audio", True):
            a = ET.SubElement(m, "audio")
            asc = ET.SubElement(a, "samplecharacteristics")
            ET.SubElement(asc, "depth").text = "16"
            ET.SubElement(asc, "samplerate").text = str(pr.get("audio_rate") or 48000)
            ET.SubElement(a, "channelcount").text = "2"


def _marker(parent, name: str, frame: int, comment: str = ""):
    m = ET.SubElement(parent, "marker")
    ET.SubElement(m, "comment").text = comment
    ET.SubElement(m, "name").text = name
    ET.SubElement(m, "in").text = str(int(frame))
    ET.SubElement(m, "out").text = "-1"


def _clip_bins(project) -> list[tuple[str, list[str]]]:
    """[(bin name, [clip ids])] by category/driver, then the special bins."""
    summ = project.clip_summaries()
    bins: dict[str, list[str]] = {}
    for c in summ:
        if c["status"] != "analysed" or c["junk"].get("is_junk"):
            continue
        if not c["drivers"]:
            bins.setdefault("_Needs review" if c["needs_review"] else "_B-roll", []).append(c["id"])
            continue
        for d in c["drivers"]:
            drv = project.data["drivers"].get(d["id"], {})
            cat = drv.get("category") or "Drivers"
            bins.setdefault(f"{cat}/{project.driver_label(d['id'])}", []).append(c["id"])
    return sorted(bins.items())


def write_fcp7_xml(project, timelines: list[Timeline], out: Path) -> Path:
    files = _Files(project)
    root = ET.Element("xmeml", version="4")
    proj = ET.SubElement(root, "project")
    ET.SubElement(proj, "name").text = f"KartCut {project.data.get('name', '')}"
    children = ET.SubElement(proj, "children")
    segs_by_clip: dict[str, list] = {}
    for sg in project.derived.get("segments", []):
        segs_by_clip.setdefault(sg["clip"], []).append(sg)
    ev_by_clip = {cid: info.get("events", []) for cid, info in project.derived.get("clips", {}).items()}

    # bins: Category > Driver
    top = ET.SubElement(children, "bin")
    ET.SubElement(top, "name").text = "KartCut drivers"
    top_children = ET.SubElement(top, "children")
    cat_nodes: dict[str, ET.Element] = {}
    mc = 0
    for name, cids in _clip_bins(project):
        if "/" in name:
            cat, drv = name.split("/", 1)
        else:
            cat, drv = None, name
        parent = top_children
        if cat:
            if cat not in cat_nodes:
                b = ET.SubElement(top_children, "bin")
                ET.SubElement(b, "name").text = cat
                cat_nodes[cat] = ET.SubElement(b, "children")
            parent = cat_nodes[cat]
        b = ET.SubElement(parent, "bin")
        ET.SubElement(b, "name").text = drv
        bc = ET.SubElement(b, "children")
        for cid in cids:
            c = project.data["clips"][cid]
            fps = float(c.get("fps") or 25)
            mc += 1
            clip = ET.SubElement(bc, "clip", id=f"masterclip-{mc}")
            ET.SubElement(clip, "name").text = c["name"]
            ET.SubElement(clip, "duration").text = str(int(round(float(c.get("duration") or 0) * fps)))
            _rate_el(clip, fps)
            media = ET.SubElement(clip, "media")
            vt = ET.SubElement(ET.SubElement(media, "video"), "track")
            ci = ET.SubElement(vt, "clipitem", id=f"clipitem-m{mc}")
            ET.SubElement(ci, "name").text = c["name"]
            _rate_el(ci, fps)
            files.el(ci, cid)
            for sg in sorted(segs_by_clip.get(cid, []), key=lambda s: s["peak"]):
                _marker(clip, sg.get("label") or sg["type"].title(), round(sg["peak"] * fps),
                        f"score {sg['score']:.2f}" + (" | incident" if sg.get("incident") else ""))
            for e in ev_by_clip.get(cid, []):
                if e["type"] == "incident":
                    _marker(clip, f"Incident: {e.get('kind', '')}", round(e["peak"] * fps), "check before use")

    tbin = ET.SubElement(children, "bin")
    ET.SubElement(tbin, "name").text = "KartCut timelines"
    tch = ET.SubElement(tbin, "children")
    for k, tl in enumerate(timelines):
        _sequence(tch, tl, files, k)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    with open(out, "wb") as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n')
        tree.write(f, encoding="utf-8", xml_declaration=False)
    return out


def _sequence(parent, tl: Timeline, files: _Files, k: int):
    fps = tl.fps
    W, H = (1920, 1080) if tl.aspect == "16:9" else (1080, 1920)
    seq = ET.SubElement(parent, "sequence", id=f"sequence-{k + 1}")
    ET.SubElement(seq, "name").text = tl.name
    ET.SubElement(seq, "duration").text = str(tl.frames)
    _rate_el(seq, fps)
    media = ET.SubElement(seq, "media")
    video = ET.SubElement(media, "video")
    fmt = ET.SubElement(ET.SubElement(video, "format"), "samplecharacteristics")
    _rate_el(fmt, fps)
    ET.SubElement(fmt, "width").text = str(W)
    ET.SubElement(fmt, "height").text = str(H)
    ET.SubElement(fmt, "pixelaspectratio").text = "square"
    vtrack = ET.SubElement(video, "track")
    audio = ET.SubElement(media, "audio")
    a1 = ET.SubElement(audio, "track")
    for i, tc in enumerate(tl.clips):
        clip_frames_seq = int(round(float(files.project.data["clips"][tc.clip_id].get("duration") or 0) * fps))
        src_in = int(round(tc.src_in * fps))
        src_out = src_in + int(round(tc.frames * tc.speed))
        for kind, track in (("video", vtrack), ("audio", a1)):
            if kind == "audio" and tc.speed != 1.0:
                continue
            ci = ET.SubElement(track, "clipitem", id=f"s{k + 1}-{kind[0]}{i + 1}")
            ET.SubElement(ci, "name").text = tc.name
            ET.SubElement(ci, "enabled").text = "TRUE"
            ET.SubElement(ci, "duration").text = str(clip_frames_seq)
            _rate_el(ci, fps)
            ET.SubElement(ci, "start").text = str(tc.start_f)
            ET.SubElement(ci, "end").text = str(tc.start_f + tc.frames)
            ET.SubElement(ci, "in").text = str(src_in)
            ET.SubElement(ci, "out").text = str(src_out)
            files.el(ci, tc.clip_id)
            st = ET.SubElement(ci, "sourcetrack")
            ET.SubElement(st, "mediatype").text = kind
            ET.SubElement(st, "trackindex").text = "1"
            if kind == "video" and tc.speed != 1.0:
                fl = ET.SubElement(ci, "filter")
                ef = ET.SubElement(fl, "effect")
                ET.SubElement(ef, "name").text = "Time Remap"
                ET.SubElement(ef, "effectid").text = "timeremap"
                ET.SubElement(ef, "effectcategory").text = "motion"
                ET.SubElement(ef, "effecttype").text = "motion"
                ET.SubElement(ef, "mediatype").text = "video"
                for pid, val in (("variablespeed", "0"), ("speed", f"{tc.speed * 100:.1f}"),
                                 ("reverse", "FALSE"), ("frameblending", "FALSE")):
                    p = ET.SubElement(ef, "parameter")
                    ET.SubElement(p, "parameterid").text = pid
                    ET.SubElement(p, "name").text = pid
                    ET.SubElement(p, "value").text = val
            if kind == "video":
                _marker(ci, tc.label, 0, f"{tc.section} | score {tc.score:.2f}")
    if tl.music and tl.music.get("path"):
        a2 = ET.SubElement(audio, "track")
        mfps = fps
        ci = ET.SubElement(a2, "clipitem", id=f"s{k + 1}-music")
        mname = Path(tl.music["path"]).name
        ET.SubElement(ci, "name").text = mname
        ET.SubElement(ci, "enabled").text = "TRUE"
        total = int(round(float(tl.music.get("duration", 0) + tl.music.get("start", 0)) * mfps))
        ET.SubElement(ci, "duration").text = str(total)
        _rate_el(ci, mfps)
        ET.SubElement(ci, "start").text = "0"
        ET.SubElement(ci, "end").text = str(tl.frames)
        m_in = int(round(float(tl.music.get("start", 0)) * mfps))
        ET.SubElement(ci, "in").text = str(m_in)
        ET.SubElement(ci, "out").text = str(m_in + tl.frames)
        f = ET.SubElement(ci, "file", id=f"file-music-{k + 1}")
        ET.SubElement(f, "name").text = mname
        ET.SubElement(f, "pathurl").text = pathurl(tl.music["path"])
        _rate_el(f, mfps)
        ET.SubElement(f, "duration").text = str(total)
        fm = ET.SubElement(ET.SubElement(f, "media"), "audio")
        ET.SubElement(ET.SubElement(fm, "samplecharacteristics"), "samplerate").text = "48000"
        ET.SubElement(fm, "channelcount").text = "2"
        st = ET.SubElement(ci, "sourcetrack")
        ET.SubElement(st, "mediatype").text = "audio"
        ET.SubElement(st, "trackindex").text = "1"
    for m in tl.markers:
        _marker(seq, m["name"], m["frame"])


# ---------------------------------------------------------------------- Resolve

def _lua_str(s) -> str:
    s = str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ")
    return f'"{s}"'


def _lua(v, indent: int = 0) -> str:
    pad = "  " * indent
    if v is None:
        return "nil"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return "0"
        return repr(v) if isinstance(v, int) else f"{v:.6g}"
    if isinstance(v, str):
        return _lua_str(v)
    if isinstance(v, dict):
        items = [f"{pad}  [{_lua_str(k)}] = {_lua(val, indent + 1)}," for k, val in v.items()]
        return "{\n" + "\n".join(items) + f"\n{pad}}}"
    if isinstance(v, (list, tuple)):
        items = [f"{pad}  {_lua(val, indent + 1)}," for val in v]
        return "{\n" + "\n".join(items) + f"\n{pad}}}"
    return _lua_str(v)


def resolve_data(project, timelines: list[Timeline]) -> dict:
    corr = (project.data.get("shot_match") or {}).get("corrections", {})
    segs_by_clip: dict[str, list] = {}
    for sg in project.derived.get("segments", []):
        segs_by_clip.setdefault(sg["clip"], []).append(sg)
    bins = []
    for name, cids in _clip_bins(project):
        clips = []
        for cid in cids:
            c = project.data["clips"][cid]
            fps = float(c.get("fps") or 25)
            info = project.derived.get("clips", {}).get(cid, {})
            markers = [{"frame": int(round(sg["peak"] * fps)), "name": sg.get("label") or sg["type"].title(),
                        "note": f"score {sg['score']:.2f}", "color": MARKER_COLOUR.get(sg["type"], "Blue")}
                       for sg in sorted(segs_by_clip.get(cid, []), key=lambda s: s["peak"])]
            markers += [{"frame": int(round(e["peak"] * fps)), "name": "Incident: " + e.get("kind", ""),
                         "note": "check before use", "color": "Purple"}
                        for e in info.get("events", []) if e["type"] == "incident"]
            lvl = info.get("exposure", {}).get("level")
            clips.append({"path": c["path"], "lut": corr.get(cid, {}).get("lut", ""), "markers": markers,
                          "color": {"bad": "Chocolate", "warn": "Tan"}.get(lvl, "")})
        bins.append({"name": name, "clips": clips})
    tls = []
    for tl in timelines:
        items = []
        for tc in tl.clips:
            fps = tc.clip_fps or tl.fps
            start = int(round(tc.src_in * fps))
            n_src = max(1, int(round(tc.frames / tl.fps * fps)))  # real-time length keeps the cuts on the beat
            items.append({"path": tc.path, "startFrame": start, "endFrame": start + n_src - 1,
                          "lut": corr.get(tc.clip_id, {}).get("lut", ""),
                          "color": SECTION_CLIP_COLOUR.get(tc.section, "Blue"),
                          "note": (f"{tc.label} | set speed {int(tc.speed * 100)}% for slow-mo"
                                   if tc.speed != 1.0 else tc.label)})
        music = None
        if tl.music and tl.music.get("path"):
            music = {"path": tl.music["path"], "start": float(tl.music.get("start", 0.0)),
                     "seconds": tl.duration}
        tls.append({"name": tl.name, "fps": tl.fps, "clips": items, "music": music,
                    "markers": [{"frame": m["frame"], "name": m["name"]} for m in tl.markers]})
    return {"title": f"KartCut {project.data.get('name', '')} {time.strftime('%H%M')}", "bins": bins,
            "timelines": tls}


RESOLVE_LUA = r'''-- KartCut -> DaVinci Resolve importer
-- Copy this file to:
--   Windows: %APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\
-- then in Resolve: Workspace > Scripts > (this file). Open the target project first.

local resolve = resolve or (Resolve and Resolve()) or (app and app:GetResolve())
if not resolve then print("KartCut: run this from Resolve's Workspace > Scripts menu") return end
local pm = resolve:GetProjectManager()
local project = pm:GetCurrentProject()
if not project then print("KartCut: open a project first") return end
local mp = project:GetMediaPool()
local root = mp:GetRootFolder()

local top = mp:AddSubFolder(root, DATA.title)
local imported = {}
local warnings = 0

local function import_into(folder, path)
  mp:SetCurrentFolder(folder)
  local res = mp:ImportMedia({path})
  local item = res and res[1]
  if item and not imported[path] then imported[path] = item end
  if not item then warnings = warnings + 1; print("KartCut: could not import " .. path) end
  return item
end

local cat_folders = {}
for _, b in ipairs(DATA.bins) do
  local parent = top
  local cat, drv = string.match(b.name, "^(.-)/(.+)$")
  if cat then
    if not cat_folders[cat] then cat_folders[cat] = mp:AddSubFolder(top, cat) end
    parent = cat_folders[cat]
  else
    drv = b.name
  end
  local folder = mp:AddSubFolder(parent, drv)
  for _, c in ipairs(b.clips) do
    local item = import_into(folder, c.path)
    if item then
      for _, m in ipairs(c.markers) do pcall(function() item:AddMarker(m.frame, m.color, m.name, m.note, 1) end) end
      if c.color ~= "" then pcall(function() item:SetClipColor(c.color) end) end
    end
  end
end

local tl_folder = mp:AddSubFolder(top, "Timelines")
for _, t in ipairs(DATA.timelines) do
  mp:SetCurrentFolder(tl_folder)
  local tl = mp:CreateEmptyTimeline(t.name)
  if tl then
    project:SetCurrentTimeline(tl)
    local infos = {}
    for _, c in ipairs(t.clips) do
      local item = imported[c.path] or import_into(tl_folder, c.path)
      if item then table.insert(infos, {mediaPoolItem = item, startFrame = c.startFrame, endFrame = c.endFrame}) end
    end
    local added = mp:AppendToTimeline(infos) or {}
    for i, ti in ipairs(added) do
      local c = t.clips[i]
      if c then
        if c.lut ~= "" then pcall(function() ti:SetLUT(1, c.lut) end) end
        pcall(function() ti:SetClipColor(c.color) end)
        pcall(function() ti:AddMarker(0, "Blue", c.note, "", 1) end)
      end
    end
    if t.music then
      local m = import_into(tl_folder, t.music.path)
      if m then
        pcall(function() tl:AddTrack("audio", "stereo") end)
        local mfps = tonumber(m:GetClipProperty("FPS")) or t.fps
        local s = math.floor(t.music.start * mfps + 0.5)
        local e = s + math.floor(t.music.seconds * mfps + 0.5) - 1
        pcall(function()
          mp:AppendToTimeline({{mediaPoolItem = m, startFrame = s, endFrame = e, mediaType = 2,
                               trackIndex = 2, recordFrame = tl:GetStartFrame()}})
        end)
      end
    end
    for _, mk in ipairs(t.markers) do pcall(function() tl:AddMarker(mk.frame, "Red", mk.name, "", 1) end) end
  end
end
print(string.format("KartCut: done. %d import warnings.", warnings))
'''


def write_resolve_script(project, timelines: list[Timeline], out: Path) -> Path:
    data = resolve_data(project, timelines)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    body = "local DATA = " + _lua(data) + "\n\n" + RESOLVE_LUA
    out.write_text(body, encoding="utf-8")
    return out


def export_all(project, timelines: list[Timeline]) -> dict:
    stamp = time.strftime("%Y%m%d_%H%M")
    base = project.root / "exports" / "nle"
    name = safe_name(project.data.get("name", "KartCut")).replace(" ", "_")
    xml = write_fcp7_xml(project, timelines, base / f"{name}_{stamp}_premiere.xml")
    lua = write_resolve_script(project, timelines, base / f"KartCut_{name}_{stamp}.lua")
    return {"xml": str(xml), "lua": str(lua), "folder": str(base)}
