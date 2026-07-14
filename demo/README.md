# Breakdown Studio demo material

Two demo fixtures ship from this directory, for two different purposes.

## 1. `make_demo_cut.py`: synthetic fixture (offline, committed)

100% procedural graphics (gradients, drifting shapes, noise fields) with burned-in slates,
show-TC and VFX notes. No real footage, no license to track, nothing to download. This is the
fixture used for CI / automated tests and for anyone who wants to run the pipeline with zero
setup:

```
python make_demo_cut.py
```

Produces `demo_cut.mp4` (~90s) and `demo_scenes_truth.csv`, both committed (small, synthetic).

## 2. `dress_film.py`: cinematic demo (real footage, reproducible, NOT committed)

For screenshots, the demo reel, and anything that needs to *look* like a real editorial handoff,
we dress a real, properly-licensed short film instead of procedural noise.

**Film:** [*Tears of Steel*](https://mango.blender.org/) by the Blender Foundation, licensed
**CC-BY 3.0**. Official download: https://download.blender.org/demo/movies/ToS/
(`tears_of_steel_1080p.mov`, ~560 MB, 1920x800, 24fps, ~12:14).

**Attribution is mandatory everywhere this footage appears**: on screenshots, in the demo reel,
and burned into the dressed movie itself:

> (CC) Blender Foundation | mango.blender.org

### Reproduce it

```bash
# 1. Download the source film to fast local disk (NOT the repo, ~560 MB, third-party footage)
mkdir -p ~/blender_demo
curl -L -o ~/blender_demo/tears_of_steel_1080p.mov \
  https://download.blender.org/demo/movies/ToS/tears_of_steel_1080p.mov

# 2. Dress it: detect shots, assign synthetic slates/notes, burn overlays + attribution
python demo/dress_film.py \
  --source ~/blender_demo/tears_of_steel_1080p.mov \
  --out ~/blender_demo/tos_dressed.mp4 \
  --truth-out demo/tos_truth.csv \
  --transnet-python <path from config.json: transnet_python> \
  --ffmpeg <path from config.json: ffmpeg> \
  --ffprobe <path from config.json: ffprobe>
```

`dress_film.py` never touches `config.json` itself (kept runnable standalone, like
`make_demo_cut.py`): pass interpreter/binary paths explicitly, or wrap the call in a small script
that reads your local `config.json` for them.

Add `--clip-seconds 180` to dress only the first N seconds while iterating (much faster than a
full-film run). Detection results and any source trim are cached under `<out-dir>/_dress_cache/`
so a re-run with the same source only re-does the burn pass.

**What ships in the repo:** `demo/tos_truth.csv` only (shot-by-shot ground truth: frame range,
synthetic slate/take, VFX note, special-slate variant). The dressed movie itself
(`tos_dressed.mp4`) is a large re-encode of third-party footage and stays on local disk, see
`demo/.gitignore`.

### Then: run the real pipeline on it

```bash
python transnet_detect.py --movie ~/blender_demo/tos_dressed.mp4 --output-base <output_base>
python bs_worker.py frames  --movie ~/blender_demo/tos_dressed.mp4 --output-base <output_base>
python bs_worker.py thumbs  --output-base <output_base> --movie ~/blender_demo/tos_dressed.mp4
python bs_worker.py qc      --output-base <output_base> --movie ~/blender_demo/tos_dressed.mp4
python bs_ocr.py slate      --frames-dir <output_base>/tos_dressed/frames --scenes-csv <scenes.csv> --config config.json
python bs_ocr.py notes      --frames-dir <output_base>/tos_dressed/frames --scenes-csv <scenes.csv> --config config.json
python contact_sheet.py --movie-stem tos_dressed --output-base <output_base> --canvas 7680x4320
```

Score the OCR output against `demo/tos_truth.csv` (`slate_display` / `note` columns) to sanity-check
the burn-in font/contrast/crop settings for a new source film.

### Result on Tears of Steel (reference run)

Full 12:14 film, 153 shots detected pre-dress / 149 shots detected on the final dressed movie
(a handful of soft cuts shift slightly across the burn-in + re-encode pass, expected and harmless).
Slate OCR: 96% exact scene/slate/take match against the truth CSV. VFX-note OCR: 98% exact match.
Both comfortably above the 90% target; the small remainder is shot-index drift between the two
independent detection passes, not a burn-in readability problem.
