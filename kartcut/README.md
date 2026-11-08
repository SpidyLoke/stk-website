# KartCut

Sorts a day of karting footage by driver, checks exposure and white balance on every clip, and cuts beat-synced highlight edits for each driver and for the whole event. Runs locally on a Windows laptop with an NVIDIA GPU. Footage never leaves the machine.

Built for Sony S-Log3 (S-Gamut3.Cine or S-Gamut3), and also handles Rec.709 and HLG clips from phones, action cams and other cameras.

## What it does

| Stage | Feature |
| --- | --- |
| Ingest | **Verified card offload**: one read of each card, written to two drives, every copy re-read and checked with xxHash64. Manifest next to each copy. Tells you when it's safe to format. |
| | **Junk filter**: clips under 1 s, lens-cap/black, blown out, accidental records (no subject, camera moving), true duplicates (same file imported twice). |
| | **Orientation**: reads the rotation flag; if a clip was shot vertical without one, works it out from the picture. |
| Sort | **Driver ID**: reads race numbers (OCR), and matches helmet, suit and kart colours (with the track and grass stripped out) plus overall appearance (CLIP). Lookalike karts are grouped so you name a whole group in one click. The match threshold is learnt from your own footage (karts sharing a frame are always different drivers). Weak matches go to *Needs review* instead of the wrong bin; every correction is learnt. |
| | **Bins**: category > driver, multi-driver, B-roll, vertical, exposure issues, incidents, junk. **Sort on disk** builds matching folders (hard links, no extra space on NTFS). |
| Colour | **Exposure analysis** in the camera's own curve: stops vs the session and absolute, clipping on the kart/driver vs background, deep shadows, exposure and white balance changes inside a clip, warm/cool drift through the day (chart). |
| | **Shot matching**: per-clip exposure + white balance correction towards a hero clip, written as S-Log3-in/S-Log3-out .cube LUTs (plus ASC CDL) and a CSV report. |
| Quality | **Panning sharpness**: kart sharp with background motion blur = sharp pan; kart soft = missed pan / soft focus. **Shake score** from camera motion, separating intentional pans from shake. |
| Highlights | **Battles and overtakes** (karts close together, positions swapping), **incidents** (sudden stop/spin, off track, possible contact, stopped kart: tagged, left out of edits unless you allow them per driver), **podium and celebrations** (arms raised, trophies, grid). |
| Edit | **Beat-synced autocut**: beats, bars and the drop from your track; slower cuts in the intro, fast cuts from the drop, podium/celebration to close. **Per-driver reels** plus one **event montage** (every driver gets in if footage allows). **Auto 9:16 reframe** that follows the kart with lead room. Slow motion from high-frame-rate clips. Engine sound under the music. |
| Hand-off | **DaVinci Resolve**: a Lua script (Workspace > Scripts) that builds driver bins, markers, clip colours, applies each clip's match LUT and builds the timelines. **Premiere**: FCP7 XML with bins, markers and timelines. |

## Install (Windows 10/11, once)

1. Unzip KartCut onto any drive with space, e.g. `D:\KartCut`. Everything it installs stays inside that folder (about 6 to 8 GB once set up): nothing goes into Windows or onto C:.
2. Double-click **`setup_windows.bat`**. It sets up a portable Python 3.12 inside the folder (your other Pythons are ignored), PyTorch for your NVIDIA card, the AI libraries, FFmpeg with NVENC, and downloads the AI models.
3. Do this at home on fast Wi-Fi. After setup KartCut works offline at the track.
4. Start it with the **KartCut** shortcut on the desktop. It opens in your browser at `http://127.0.0.1:8765`. Keep the black window open while you work.

Update your NVIDIA driver first if it is old; the installer picks the PyTorch build your driver supports and falls back to CPU (slower) if none fits.

## Race day workflow

1. **Project**: click the project button (top left) > create one per event.
2. **Ingest**: add each card, pick Drive A and Drive B, start offload. Format cards only after the green "safe to format" banner. The Drive A copy is added to the project automatically.
3. **Analyse new clips**. You can keep shooting; add new folders and analyse again later, finished clips are not redone.
4. **Drivers**: paste the entry list (number, name, category per line, straight from Excel or the timing sheet). Fill in kart numbers: any number KartCut reads then sorts that clip on its own. On the right, **Groups** shows karts that look alike (helmet, suit, kart colours). Pick a driver from **This is…** and the whole group is sorted; pick **Other racer** for competitors you don't need. KartCut re-matches everything else after each one. Set incidents to *Leave out* for anyone who shouldn't have their crash in a reel.
5. **Clips**: work through *Needs review*. A clip in the wrong driver's bin: open it and use **Move to…** (or **Also in…** when two of your drivers are in shot), or drag clips onto a driver on the left. Ctrl+click or tick several clips to move them together. Every move teaches KartCut. If you already sorted to folders, run **Sort into driver folders** again and moved clips leave their old folder (only files KartCut put there are touched, never your footage).
6. **Colour**: pick a hero clip (or let it auto-pick), generate match LUTs and the exposure report.
7. **Montage**: pick a track, analyse beats, choose event montage and/or per-driver reels, 16:9 and/or 9:16, plan, check the strips, render. Use *Shuffle* for different picks. *Export to Resolve + Premiere* gives you the same edits as editable timelines.

### Shooting habits that make it more accurate

- Get one clean side or front shot of every kart with the number readable (grid or pit lane is perfect). Use those as reference shots.
- Shoot 100p/120p for slow motion; KartCut slows those clips in intros, outros and beauty pans.
- Set camera clocks before the day so clips sort in true shooting order.

## Using the Resolve script

1. Copy the `.lua` file from `exports/nle/` to `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\`.
2. Open (or create) your Resolve project.
3. Workspace > Scripts > the KartCut file.

It works in the free version. Resolve's scripting API can't set clip speed, so slow-motion shots come in at normal speed with a marker telling you to set 50%.

## Where things are saved

Each project folder (default `KartCut\projects\<project>`, or wherever you choose when creating it) holds:

- `renders/`: finished edits (H.264, 1080p or 1080x1920)
- `exports/shot_match/`: match LUTs, CDLs, `exposure_report.csv`
- `exports/nle/`: Resolve script and Premiere XML
- `cache/`: analysis, thumbnails, preview proxies (safe to delete; re-analyse to rebuild)

## How it decides (method level)

- **Exposure**: frames are decoded in the camera's log curve, converted to scene-linear light with Sony's published S-Log3 maths, and measured against the dominant neutral surface in frame (usually the asphalt). That gives stops, not just brightness, so a -2 stop clip reads as -2.0.
- **White balance**: estimated from the same neutral surface, reported as colour temperature and mired shift vs the rest of the session.
- **Driver ID**: per kart, a probability for every driver plus "unknown", combining number reads, colour fingerprints and appearance similarity to your reference shots.
- **Highlights**: kart size, real speed (camera motion removed), closeness of karts, overtakes, celebrations, then weighted by sharpness, stability and exposure.
- **Edit**: cut points on beats; shot length per section in whole bars/half-bars; best segment per slot by type fit, quality and variety (no repeat clips, drivers spread out).

## Tested so far

Tested in a Linux container on a synthetic S-Log3 shoot (19 automated tests in `tests/`): offload + corruption detection, junk/duplicate filter, S-Log3 exposure maths, warm cast detection, shot-match LUTs, sharp-pan / soft-focus / shaky flags, overtake and battle detection, OCR of real rendered kart numbers, driver ID and learning from corrections, beat detection, planning, rendering 16:9 and 9:16, Premiere XML, the Resolve script against a mock Resolve API, sorting on disk, and the web UI.

**Not yet tested**: real karting footage, the YOLO-World "go-kart" detector (its model host was blocked in the test container), CLIP appearance matching, NVENC/NVDEC on a real NVIDIA GPU, and importing into an actual Resolve or Premiere. The first real day is the real test: check *Needs review*, and expect to tune.

## Troubleshooting

- **Chips at the top say "CPU only"**: PyTorch can't see the GPU. Update the NVIDIA driver and run setup again.
- **Detector shows "YOLO11 COCO (fallback)"**: YOLO-World didn't load (usually CLIP missing). Run setup again; the fallback still works, less accurately.
- **A clip is sideways or the wrong profile**: open it in Clips > Re-analyse as rotated / pick the profile.
- **Sort on disk fails with links**: exFAT drives can't hold hard links. Pick a folder on the same NTFS drive as the footage, or choose Copy.
- Logs: `KartCut\projects\logs\kartcut.log`.
- **Setup says Python failed its self-check**: antivirus is blocking files in `KartCut\python`. Allow the KartCut folder in your antivirus, delete the `python` folder, run setup again.

## Run tests (developers)

```
pip install pytest lupa
python -m pytest tests -q
```
