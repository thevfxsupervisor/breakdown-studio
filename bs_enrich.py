#!/usr/bin/env python
"""bs_enrich.py - dialogue transcription + AI shot-description enrichment for Breakdown Studio.

CONFIDENTIALITY (non-negotiable): enrichment runs on LOCAL endpoints ONLY by default
(localhost / a LAN Ollama box you point --ollama-url at). Frames and dialogue of unreleased
footage must NEVER be sent to a cloud service. Prompts built here NEVER inject real people's
names -- character names that appear in output are derived ONLY from on-screen dialogue
(someone addressed by name), never from an operator-supplied cast list. See build_pass2_prompt().

Reads the same frames/ + Scenes.csv layout bs_worker.py produces and follows its PROGRESS/LOG
line protocol so the GUI can drive a progress bar. Self-contained aside from two OPTIONAL, lazily
imported deps:
  faster-whisper   (transcribe)   pip install faster-whisper
  requests         (describe)     pip install requests   (talks to a local Ollama HTTP server)

Stages:
  transcribe   movie audio -> dialogue.csv (tcid, start, end, dialogue) + transcript.txt
  describe     frames + dialogue.csv -> descriptions.csv (tcid, visual_caption, revised_description)
               + story_arc.txt. Two passes:
                 pass 1 (visual): mid-frame -> local Ollama vision model -> one-line caption
                 pass 2 (story):  chunks of ~25 shots (captions + dialogue) -> local Ollama text
                                  model -> narrative-aware revised description + rolling story state

CLI:
  python bs_enrich.py transcribe --movie M --output-base B [--config C]
  python bs_enrich.py describe   --movie M --output-base B [--config C] [--pass1-only] [--force]

Config keys (all optional, config.json):
  whisper_model   faster-whisper model size, default "small"
  ollama_url      default "http://localhost:11434"
  ollama_urls     optional list of fallback Ollama URLs (e.g. a LAN GPU box), tried in order
  vision_model    default "llava:13b"
  text_model      default "llama3.1"
  prefix          shot-code prefix (matches bs_worker), default "SHW"
  fps             override fps (default: probed from --movie, else 24.0)

Both stages print 'PROGRESS <stage> done/total' lines (matching bs_worker.py / bs_ocr.py) and a
final 'DONE <stage>' line.
"""
import argparse
import base64
import csv
import io
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bs_worker as W  # noqa: E402  (reuse tcid / Scenes.csv parsing so both modules agree)

DEFAULT_PREFIX = "SHW"
DEFAULT_WHISPER_MODEL = "small"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_VISION_MODEL = "llava:13b"
DEFAULT_TEXT_MODEL = "llama3.1"
VISION_MAX_SIDE = 768
CHUNK_SIZE = 25
CHUNK_OVERLAP = 3


def progress(stage, done, total):
    print(f"PROGRESS {stage} {done}/{total}", flush=True)


def log(msg):
    print(f"LOG {msg}", flush=True)


# --------------------------------------------------------------------------------------- config

def load_config(config_path):
    cfg = {}
    if config_path and Path(config_path).exists():
        cfg.update(json.loads(Path(config_path).read_text(encoding="utf-8")))
    if os.environ.get("BS_PREFIX"):
        cfg["prefix"] = os.environ["BS_PREFIX"]
    return cfg


def get_prefix(cfg):
    return cfg.get("prefix") or os.environ.get("BS_PREFIX") or DEFAULT_PREFIX


def get_fps(cfg):
    try:
        return float(cfg.get("fps") or 24.0)
    except (TypeError, ValueError):
        return 24.0


def ollama_urls(cfg, cli_url=None):
    """Ordered list of Ollama base URLs to try: CLI override, then config ollama_url, then any
    config ollama_urls fallbacks, then the hardcoded localhost default. De-duplicated, order kept."""
    urls = []
    if cli_url:
        urls.append(cli_url)
    if cfg.get("ollama_url"):
        urls.append(cfg["ollama_url"])
    for u in cfg.get("ollama_urls", []) or []:
        urls.append(u)
    urls.append(DEFAULT_OLLAMA_URL)
    out = []
    for u in urls:
        u = (u or "").rstrip("/")
        if u and u not in out:
            out.append(u)
    return out


# --------------------------------------------------------------------------------------- shots

def _load_shots(output_dir, stem, fps, prefix):
    csv_file = W.find_scenes_csv(output_dir, stem)
    if not csv_file:
        sys.exit(f"ERROR: no Scenes.csv found under {output_dir} (run detection + bs_worker frames first).")
    scenes = W.parse_scenes_csv(csv_file)
    for s in scenes:
        s["tcid"] = W.tc_to_id(s["start_tc"], fps)
    return scenes


# =================================================================================================
# FEATURE 1: transcribe
# =================================================================================================

def _get_whisper_model(model_size):
    """Lazy import + construct faster-whisper. Auto device: cuda if available else cpu int8."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise SystemExit(
            "bs_enrich transcribe requires the 'faster-whisper' package.\n"
            "Install it in the worker environment with:\n"
            "    pip install faster-whisper\n"
            f"(import failed: {e})"
        )
    device, compute_type = "cpu", "int8"
    try:
        import torch
        if torch.cuda.is_available():
            device, compute_type = "cuda", "float16"
    except ImportError:
        pass
    log(f"loading faster-whisper model '{model_size}' on {device}/{compute_type}")
    return WhisperModel(model_size, device=device, compute_type=compute_type)


def transcribe_audio(movie_path, model_size):
    """Run faster-whisper once over the movie's audio track. Returns a list of segment dicts:
    {"start": float_seconds, "end": float_seconds, "text": str}, plus the detected language."""
    model = _get_whisper_model(model_size)
    segments_iter, info = model.transcribe(str(movie_path), beam_size=5, vad_filter=True)
    segments = []
    for seg in segments_iter:
        text = (seg.text or "").strip()
        if text:
            segments.append({"start": float(seg.start), "end": float(seg.end), "text": text})
    log(f"transcribed {len(segments)} segments (language={getattr(info, 'language', '?')})")
    return segments


def map_segments_to_shots(segments, shots, fps):
    """Assign each dialogue segment to every shot it overlaps, then join per-shot text in order.

    shots: list of {"tcid", "start_frame", "end_frame", ...} as parsed from Scenes.csv (1-based,
    end-inclusive frame numbers, matching bs_worker.parse_scenes_csv / shot_frame_indices).
    segments: list of {"start","end","text"} in SECONDS (whisper's native unit).

    Overlap test: a segment overlaps a shot if segment.start < shot_end_s and segment.end >
    shot_start_s (half-open interval overlap; a segment that ends exactly on a shot boundary does
    NOT count as touching the next shot, avoiding double-counting from adjoining shots).

    Returns {tcid: {"start": shot_start_s, "end": shot_end_s, "dialogue": joined_text}} for every
    shot (dialogue is "" when nothing overlaps).
    """
    out = {}
    for s in shots:
        # Scenes.csv frames are 1-based inclusive; shot spans [start_frame-1, end_frame) in 0-based
        # half-open seconds (end_frame is the first frame of the NEXT shot in a continuous cut).
        shot_start_s = (s["start_frame"] - 1) / fps
        shot_end_s = s["end_frame"] / fps
        pieces = []
        for seg in segments:
            if seg["start"] < shot_end_s and seg["end"] > shot_start_s:
                pieces.append(seg["text"])
        out[s["tcid"]] = {
            "start": shot_start_s,
            "end": shot_end_s,
            "dialogue": " ".join(pieces).strip(),
        }
    return out


def write_dialogue_csv(path, shots, mapping, prefix):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["tcid", "shot_code", "start", "end", "dialogue"])
        for s in shots:
            m = mapping.get(s["tcid"], {"start": 0.0, "end": 0.0, "dialogue": ""})
            w.writerow([s["tcid"], f"{prefix}_{s['tcid']}", f"{m['start']:.3f}", f"{m['end']:.3f}",
                       m["dialogue"]])


def _fmt_ts(seconds):
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def write_transcript_txt(path, segments):
    lines = []
    for seg in segments:
        lines.append(f"[{_fmt_ts(seg['start'])} --> {_fmt_ts(seg['end'])}] {seg['text']}")
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def stage_transcribe(movie, output_dir, shots, fps, prefix, whisper_model):
    if not shots:
        log("no shots to transcribe against")
        progress("transcribe", 0, 0)
        return
    progress("transcribe", 0, 1)
    segments = transcribe_audio(movie, whisper_model)
    progress("transcribe", 1, 1)
    mapping = map_segments_to_shots(segments, shots, fps)
    dialogue_csv = output_dir / "dialogue.csv"
    write_dialogue_csv(dialogue_csv, shots, mapping, prefix)
    transcript_txt = output_dir / "transcript.txt"
    write_transcript_txt(transcript_txt, segments)
    with_dialogue = sum(1 for m in mapping.values() if m["dialogue"])
    print(f"[transcribe] {len(shots)} shots, {with_dialogue} with dialogue, "
          f"{len(segments)} segments -> {dialogue_csv.name}, {transcript_txt.name}", flush=True)


# =================================================================================================
# FEATURE 2: describe (two-pass)
# =================================================================================================

def load_dialogue_csv(path):
    """dialogue.csv (written by stage_transcribe) -> {tcid: dialogue_text}. Missing file -> {}
    (describe can run without transcribe having been run; dialogue is simply blank everywhere)."""
    out = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            tcid = (row.get("tcid") or "").strip()
            if tcid:
                out[tcid] = (row.get("dialogue") or "").strip()
    return out


def load_descriptions_csv(path):
    """Existing descriptions.csv -> {tcid: row_dict}, for resume-skip. Missing file -> {}."""
    out = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            tcid = (row.get("tcid") or "").strip()
            if tcid:
                out[tcid] = row
    return out


def shots_needing_pass1(shots, existing, force):
    """Resume logic: a shot needs pass-1 unless it's already in the CSV with a non-empty
    visual_caption (and --force wasn't passed)."""
    if force:
        return list(shots)
    return [s for s in shots
            if not (existing.get(s["tcid"], {}).get("visual_caption") or "").strip()]


def shots_needing_pass2(shots, existing, force):
    """Resume logic for pass 2: a shot needs revision unless it already has a non-empty
    revised_description (and --force wasn't passed). Shots with no visual_caption at all can't be
    revised yet (pass 1 must run first) and are excluded here; caller re-checks after pass 1."""
    if force:
        return list(shots)
    return [s for s in shots
            if not (existing.get(s["tcid"], {}).get("revised_description") or "").strip()]


# ---- image prep ---------------------------------------------------------------------------------

def _mid_frame_path(output_dir, tcid, prefix):
    for sub in ("frames", "thumbs"):
        p = output_dir / sub / f"{prefix}_{tcid}-mid.jpg"
        if p.exists():
            return p
    return None


def frame_to_b64(path, max_side=VISION_MAX_SIDE):
    """Downscale a frame to ~max_side on its long edge and return base64-encoded JPEG bytes
    (str, no data: prefix -- Ollama's /api/generate 'images' field wants raw base64)."""
    from PIL import Image
    im = Image.open(path).convert("RGB")
    w, h = im.size
    scale = max_side / max(w, h)
    if scale < 1.0:
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=88)
    return base64.b64encode(buf.getvalue()).decode("ascii")


# ---- Ollama HTTP calls ---------------------------------------------------------------------------

def _ollama_generate(base_url, model, prompt, images=None, timeout=120):
    """POST /api/generate (non-streaming) -> response text. Raises on transport/HTTP failure so
    the caller's multi-URL fallback loop can try the next endpoint."""
    import requests
    payload = {"model": model, "prompt": prompt, "stream": False}
    if images:
        payload["images"] = images
    r = requests.post(f"{base_url}/api/generate", json=payload, timeout=timeout)
    r.raise_for_status()
    return (r.json().get("response") or "").strip()


def ollama_generate_with_fallback(urls, model, prompt, images=None, timeout=120):
    """Try each URL in order; return (text, url_used). Raises the last error if all fail."""
    try:
        import requests  # noqa: F401
    except ImportError as e:
        raise SystemExit(
            "bs_enrich describe requires the 'requests' package to talk to Ollama.\n"
            "Install it in the worker environment with:\n"
            "    pip install requests\n"
            f"(import failed: {e})"
        )
    last_err = None
    for url in urls:
        try:
            text = _ollama_generate(url, model, prompt, images=images, timeout=timeout)
            return text, url
        except Exception as e:
            last_err = e
            log(f"Ollama endpoint {url} failed ({e}); trying next")
    raise SystemExit(f"ERROR: all Ollama endpoints failed for model '{model}'. Last error: {last_err}")


# ---- pass 1: visual caption ----------------------------------------------------------------------

def build_pass1_prompt():
    """Terse factual one-line shot description prompt: subject, action, setting, shot size.
    No character names, no speculation -- this is a purely visual read of a single frame."""
    return (
        "You are describing a single frame from a film shot, for a VFX shot-breakdown sheet. "
        "Write ONE factual line describing: the subject, the action, the setting, and the shot "
        "size (e.g. wide, medium, close-up). Do not guess names, dialogue, emotions, or backstory "
        "-- describe only what is visibly in the frame. No preamble, no quotes, one line only."
    )


def run_pass1(output_dir, shots, urls, vision_model, prefix, existing, force,
              all_shots=None, checkpoint_path=None, checkpoint_every=20):
    """all_shots + checkpoint_path (optional): if given, the descriptions.csv is re-written every
    `checkpoint_every` shots (and once more at the end) so a mid-run kill (background-task
    timeout, crash, Ctrl-C) loses at most `checkpoint_every` shots of work -- the resume-skip logic
    (shots_needing_pass1) picks up from the last checkpoint on the next invocation."""
    todo = shots_needing_pass1(shots, existing, force)
    total = len(todo)
    if total == 0:
        log("pass1: nothing to do (all shots already have a visual_caption; use --force to redo)")
        progress("describe_pass1", 0, 0)
        return dict(existing)
    prompt = build_pass1_prompt()
    result = dict(existing)
    done = 0
    for s in todo:
        tcid = s["tcid"]
        frame = _mid_frame_path(output_dir, tcid, prefix)
        if frame is None:
            log(f"pass1: no mid frame for {tcid}, skipping")
            done += 1
            continue
        img_b64 = frame_to_b64(frame)
        text, url_used = ollama_generate_with_fallback(urls, vision_model, prompt, images=[img_b64])
        row = dict(result.get(tcid, {}))
        row["tcid"] = tcid
        row["visual_caption"] = text.replace("\n", " ").strip()
        row.setdefault("revised_description", row.get("revised_description", ""))
        result[tcid] = row
        done += 1
        if done % 5 == 0 or done == total:
            progress("describe_pass1", done, total)
        if checkpoint_path is not None and all_shots is not None and done % checkpoint_every == 0:
            write_descriptions_csv(checkpoint_path, all_shots, result, prefix)
            log(f"pass1: checkpointed {done}/{total} to {checkpoint_path.name}")
    print(f"[describe] pass1 (visual): {done}/{total} shots captioned via {vision_model}", flush=True)
    return result


# ---- pass 2: story-context revision ---------------------------------------------------------------

def chunk_shots(shots, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """Split an ordered shot list into overlapping chunks of ~chunk_size shots.

    Each chunk after the first repeats the last `overlap` shots of the previous chunk (so the text
    model sees continuity context), but the CALLER is responsible for only writing revised
    descriptions for the "new" portion of each chunk (see run_pass2) -- chunk_shots itself just
    returns the windows.

    Returns a list of (chunk_shots_list, new_start_idx) where new_start_idx is the index within the
    chunk (0-based) at which shots are "new" to this chunk (0 for the first chunk).
    """
    n = len(shots)
    if n == 0:
        return []
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    step = max(1, chunk_size - overlap)
    chunks = []
    i = 0
    while i < n:
        window = shots[i:i + chunk_size]
        new_start = overlap if (i > 0 and chunk_size > overlap) else 0
        new_start = min(new_start, len(window))
        chunks.append((window, new_start))
        if i + chunk_size >= n:
            break
        i += step
    return chunks


_NAME_TOKEN_RE = None  # placeholder kept for readability; regex built inline in extract_addressed_names


def extract_addressed_names(dialogue_texts):
    """Heuristic extraction of DIALOGUE-DERIVED character names: a capitalized word/phrase that
    appears to be someone being directly addressed (vocative), e.g. '...isn't that right, Sam?'
    or 'Mrs. Carter, wait!'. This is only used to SEED the pass-2 prompt with names actually spoken
    on screen -- it never reads from any operator/cast list, and the caller passes only this
    derived set into the prompt (see build_pass2_prompt).

    Deliberately conservative (misses plenty of real vocatives) -- false negatives are fine here,
    the model does its own read of the dialogue text too; this just gives it a starting set.
    """
    import re
    names = set()
    pattern = re.compile(
        r"(?:^|[,\.\!\?]\s*)((?:Mrs?\.|Ms\.|Dr\.)?\s*[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s*[,\.\!\?]"
    )
    stop = {"The", "A", "An", "It", "This", "That", "He", "She", "They", "We", "You", "I"}
    for text in dialogue_texts:
        if not text:
            continue
        for m in pattern.finditer(text):
            cand = m.group(1).strip()
            first_word = cand.split()[0].rstrip(".")
            if first_word in stop:
                continue
            if len(cand) < 2:
                continue
            names.add(cand)
    return sorted(names)


def build_pass2_prompt(chunk_rows, story_state, known_names):
    """Assemble the pass-2 (story context) prompt for one chunk.

    chunk_rows: list of {"tcid","shot_code","visual_caption","dialogue"} for the chunk, in order.
    story_state: rolling free-text summary from the previous chunk ("" for the first chunk).
    known_names: sorted list of character names extracted from DIALOGUE so far (extract_addressed_
                 names output, accumulated across chunks) -- NEVER an operator-supplied cast list.

    Returns the prompt string. Callers must not pass any operator/production names list into this
    function -- the only names available to the model are ones this module derived from dialogue.
    """
    lines = []
    lines.append(
        "You are a story analyst helping revise shot descriptions for a film's VFX breakdown. "
        "You will see a sequence of shots (visual caption + any spoken dialogue) in screen order."
    )
    lines.append(
        "Identify recurring characters ONLY by names that are actually spoken in the dialogue "
        "below (someone addressed by name on screen). Do not invent or assume any name that is "
        "not present in the dialogue text you are given."
    )
    if known_names:
        lines.append("Names heard in dialogue so far: " + ", ".join(known_names))
    lines.append("")
    lines.append("STORY SO FAR:")
    lines.append(story_state.strip() if story_state else "(this is the first sequence)")
    lines.append("")
    lines.append("SHOTS IN THIS SEQUENCE:")
    for row in chunk_rows:
        dlg = row.get("dialogue") or ""
        dlg_part = f' | dialogue: "{dlg}"' if dlg else ""
        lines.append(f"- {row['shot_code']}: {row.get('visual_caption', '')}{dlg_part}")
    lines.append("")
    lines.append(
        "Respond in exactly this format, nothing else:\n"
        "STORY_STATE: <one short paragraph updating the rolling story-so-far summary>\n"
        "REVISED:\n"
        "<shot_code>: <one factual line, narrative-aware, no speculation markers like 'possibly' "
        "or 'maybe'>\n"
        "(one REVISED line per shot, in the same order, using the exact shot_code given)"
    )
    return "\n".join(lines)


def parse_pass2_response(text, chunk_codes):
    """Parse the model's STORY_STATE / REVISED block. Tolerant of minor formatting drift: matches
    each shot_code that appears at the start of a line, in any order, and returns
    (story_state_str, {shot_code: revised_line}). Shots the model dropped are simply absent from
    the returned dict -- callers keep the pass-1 caption as fallback."""
    import re
    story_state = ""
    m = re.search(r"STORY_STATE:\s*(.+?)(?:\nREVISED:|\Z)", text, re.DOTALL | re.IGNORECASE)
    if m:
        story_state = m.group(1).strip()
    revised = {}
    code_set = set(chunk_codes)
    for line in text.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        code, _, rest = line.partition(":")
        code = code.strip().lstrip("-").strip()
        if code in code_set:
            revised[code] = rest.strip()
    return story_state, revised


def run_pass2(shots, urls, text_model, existing, force, story_arc_path,
             all_shots=None, checkpoint_path=None, prefix=None):
    """Runs pass 2 over shot rows that already have a visual_caption (pass 1 must have populated
    `existing` first). Returns (updated_rows_dict, story_arc_entries).

    all_shots + checkpoint_path + prefix (optional): if given, descriptions.csv is re-written
    after every chunk so a mid-run kill loses at most one chunk's worth of revisions (same
    checkpoint contract as run_pass1)."""
    shots_with_caption = [s for s in shots
                          if (existing.get(s["tcid"], {}).get("visual_caption") or "").strip()]
    todo = shots_needing_pass2(shots_with_caption, existing, force)
    if not todo:
        log("pass2: nothing to do (all captioned shots already revised; use --force to redo)")
        progress("describe_pass2", 0, 0)
        return dict(existing), []

    todo_tcids = {s["tcid"] for s in todo}
    result = dict(existing)
    chunks = chunk_shots(shots_with_caption, CHUNK_SIZE, CHUNK_OVERLAP)
    total = len(chunks)
    story_state = ""
    known_names = []
    arc_entries = []

    for ci, (window, new_start) in enumerate(chunks, 1):
        # Only bother calling the model if this chunk actually contains a shot still needing work.
        window_tcids = {s["tcid"] for s in window}
        if not (window_tcids & todo_tcids):
            progress("describe_pass2", ci, total)
            continue
        chunk_rows = []
        dialogue_texts = []
        for s in window:
            row = result.get(s["tcid"], {})
            dlg = row.get("dialogue", "") or ""
            dialogue_texts.append(dlg)
            chunk_rows.append({
                "tcid": s["tcid"],
                "shot_code": row.get("shot_code") or s["tcid"],
                "visual_caption": row.get("visual_caption", ""),
                "dialogue": dlg,
            })
        new_names = extract_addressed_names(dialogue_texts)
        for n in new_names:
            if n not in known_names:
                known_names.append(n)
        prompt = build_pass2_prompt(chunk_rows, story_state, sorted(known_names))
        text, url_used = ollama_generate_with_fallback(urls, text_model, prompt)
        new_story_state, revised = parse_pass2_response(text, [r["shot_code"] for r in chunk_rows])
        if new_story_state:
            story_state = new_story_state
            arc_entries.append(f"[sequence {ci}] {story_state}")
        for r in chunk_rows:
            code = r["shot_code"]
            tcid = r["tcid"]
            line = revised.get(code, "").strip()
            row = dict(result.get(tcid, {}))
            row["tcid"] = tcid
            row.setdefault("shot_code", code)
            row["visual_caption"] = r["visual_caption"]
            row.setdefault("dialogue", r["dialogue"])
            if line:
                row["revised_description"] = line
            else:
                row.setdefault("revised_description", row.get("visual_caption", ""))
            result[tcid] = row
        progress("describe_pass2", ci, total)
        if checkpoint_path is not None and all_shots is not None:
            write_descriptions_csv(checkpoint_path, all_shots, result, prefix or "SHW")
            log(f"pass2: checkpointed sequence {ci}/{total} to {checkpoint_path.name}")

    revised_count = sum(1 for t in todo_tcids
                        if (result.get(t, {}).get("revised_description") or "").strip())
    print(f"[describe] pass2 (story): {len(chunks)} sequence chunk(s), "
          f"{revised_count}/{len(todo)} shots revised via {text_model}", flush=True)
    return result, arc_entries


def write_descriptions_csv(path, shots, rows, prefix):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["tcid", "shot_code", "visual_caption", "revised_description"])
        for s in shots:
            r = rows.get(s["tcid"], {})
            w.writerow([s["tcid"], f"{prefix}_{s['tcid']}", r.get("visual_caption", ""),
                       r.get("revised_description", "")])


def stage_describe(movie, output_dir, shots, urls, vision_model, text_model, prefix, pass1_only,
                    force):
    if not shots:
        log("no shots to describe")
        progress("describe_pass1", 0, 0)
        return
    desc_csv = output_dir / "descriptions.csv"
    existing_rows = load_descriptions_csv(desc_csv)
    dialogue_by_tcid = load_dialogue_csv(output_dir / "dialogue.csv")

    # seed shot_code + dialogue into every row up front so pass2 always has dialogue available,
    # even for shots that already had a visual_caption from a prior run.
    for s in shots:
        row = dict(existing_rows.get(s["tcid"], {}))
        row["tcid"] = s["tcid"]
        row.setdefault("shot_code", f"{prefix}_{s['tcid']}")
        row["dialogue"] = dialogue_by_tcid.get(s["tcid"], "")
        existing_rows[s["tcid"]] = row

    rows = run_pass1(output_dir, shots, urls, vision_model, prefix, existing_rows, force,
                     all_shots=shots, checkpoint_path=desc_csv)
    write_descriptions_csv(desc_csv, shots, rows, prefix)

    if pass1_only:
        print(f"[describe] --pass1-only: wrote {desc_csv.name} (visual captions only)", flush=True)
        return

    rows, arc_entries = run_pass2(shots, urls, text_model, rows, force, output_dir / "story_arc.txt",
                                  all_shots=shots, checkpoint_path=desc_csv, prefix=prefix)
    write_descriptions_csv(desc_csv, shots, rows, prefix)
    if arc_entries:
        arc_path = output_dir / "story_arc.txt"
        prior = arc_path.read_text(encoding="utf-8") if arc_path.exists() and not force else ""
        arc_path.write_text((prior + "\n" if prior else "") + "\n".join(arc_entries) + "\n",
                            encoding="utf-8")
    print(f"[describe] wrote {desc_csv.name} ({len(shots)} shots)", flush=True)


# =================================================================================================
# CLI
# =================================================================================================

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="stage", required=True)

    def common(p):
        p.add_argument("--movie", required=True)
        p.add_argument("--output-base", required=True)
        p.add_argument("--config", default="", help="path to config.json")
        p.add_argument("--prefix", default=None, help="override cfg/BS_PREFIX shot-code prefix")
        p.add_argument("--fps", type=float, default=None, help="override cfg fps (default 24.0)")

    p1 = sub.add_parser("transcribe")
    common(p1)
    p1.add_argument("--whisper-model", default=None, help="override cfg whisper_model (default 'small')")

    p2 = sub.add_parser("describe")
    common(p2)
    p2.add_argument("--pass1-only", action="store_true", help="run only the visual captioning pass")
    p2.add_argument("--force", action="store_true", help="re-process shots even if already in descriptions.csv")
    p2.add_argument("--ollama-url", default=None, help="override cfg ollama_url (default http://localhost:11434)")
    p2.add_argument("--vision-model", default=None, help="override cfg vision_model (default 'llava:13b')")
    p2.add_argument("--text-model", default=None, help="override cfg text_model (default 'llama3.1')")

    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.prefix:
        cfg["prefix"] = args.prefix
    if args.fps:
        cfg["fps"] = args.fps

    prefix = get_prefix(cfg)
    movie = Path(args.movie)
    stem = movie.stem
    output_dir = Path(args.output_base) / stem
    output_dir.mkdir(parents=True, exist_ok=True)

    # fps precedence: --fps/config, else probe the real movie (matches bs_worker.py's own CLI so
    # both modules agree on frame-second math for the same cut), else the 24.0 fallback. Probing
    # needs BS_FFPROBE (or a plain 'ffprobe' on PATH) -- if neither resolves, fall back quietly
    # rather than crash the whole stage over an fps guess.
    if args.fps or cfg.get("fps"):
        fps = get_fps(cfg)
    elif movie.exists():
        try:
            fps = W.get_fps(movie)
        except OSError as e:
            log(f"ffprobe unavailable ({e}); falling back to 24.0fps -- pass --fps or set "
                f"BS_FFPROBE for an accurate probe")
            fps = 24.0
    else:
        fps = 24.0

    print(f"=== bs_enrich {args.stage} | {stem} @ {fps:.3f}fps ===", flush=True)
    shots = _load_shots(output_dir, stem, fps, prefix)
    log(f"loaded {len(shots)} shots")

    if args.stage == "transcribe":
        whisper_model = args.whisper_model or cfg.get("whisper_model") or DEFAULT_WHISPER_MODEL
        stage_transcribe(movie, output_dir, shots, fps, prefix, whisper_model)
    elif args.stage == "describe":
        vision_model = args.vision_model or cfg.get("vision_model") or DEFAULT_VISION_MODEL
        text_model = args.text_model or cfg.get("text_model") or DEFAULT_TEXT_MODEL
        urls = ollama_urls(cfg, args.ollama_url)
        stage_describe(movie, output_dir, shots, urls, vision_model, text_model, prefix,
                       args.pass1_only, args.force)

    print(f"DONE {args.stage}", flush=True)


if __name__ == "__main__":
    main()
