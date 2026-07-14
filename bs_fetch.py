#!/usr/bin/env python
"""bs_fetch.py - video-URL fetch stage for Breakdown Studio.

Downloads a video URL (YouTube, Vimeo, or any site yt-dlp supports; also plain direct
http(s) links to a media file) into the configured output_base as a local movie file, so
the rest of the pipeline (detect/frames/thumbs/...) can take over exactly as if the user
had picked a local file.

yt-dlp is an OPTIONAL dependency, imported lazily. If it isn't installed:
  - a direct http(s) link to a media file (path ends in a known video extension, or the
    server's Content-Type says so) still works via a plain urllib streaming download.
  - anything else (a YouTube/Vimeo page URL etc.) prints a friendly one-liner and exits
    non-zero: "pip install yt-dlp".

Re-running with the same URL reuses the existing downloaded file (prints SKIP, not
FETCHED) unless --force is passed, matching the rest of the pipeline's "safe to re-run"
convention.

CLI:
  python bs_fetch.py --url URL --output-base DIR [--config config.json]
                      [--format best-mp4-preference] [--force]

Prints 'PROGRESS fetch <done>/<total>' lines (matching bs_worker.py's protocol; fetch has
exactly one unit of work, so this is just 0/1 then 1/1) and a final line the GUIs parse:
  FETCHED <absolute path>
or, on a same-URL re-run:
  SKIP <absolute path>
"""
import argparse
import json
import os
import re
import sys
import unicodedata
import urllib.request
from pathlib import Path

FETCH_DIR_NAME = "_fetched"

# extensions treated as "this is already a media file, no yt-dlp needed" for the plain-
# download fallback path.
DIRECT_MEDIA_EXTS = (
    ".mp4", ".mov", ".mkv", ".mxf", ".avi", ".webm", ".m4v", ".mpg", ".mpeg",
)


def progress(stage, done, total):
    print(f"PROGRESS {stage} {done}/{total}", flush=True)


def load_config(config_path):
    cfg = {}
    if config_path and Path(config_path).exists():
        cfg.update(json.loads(Path(config_path).read_text(encoding="utf-8")))
    return cfg


def is_url(s):
    return bool(s) and s.strip().lower().startswith(("http://", "https://"))


def sanitize_filename(title, fallback="video"):
    """Turn a video title (or anything) into a safe filesystem stem: strip accents down to
    ASCII where possible, drop characters Windows/macOS/Linux all reject, collapse
    whitespace, cap the length so it never trips a MAX_PATH-ish limit."""
    title = (title or "").strip() or fallback
    # normalize accents to their closest ASCII form rather than dropping the character
    # entirely (e.g. "é" -> "e")
    title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title)
    title = re.sub(r"\s+", " ", title).strip(" .")
    title = title or fallback
    return title[:120]


def _existing_download(fetch_dir, stem):
    """Return the path of an already-downloaded file for this stem, if any (any extension:
    yt-dlp picks the container; a direct link keeps its own)."""
    if not fetch_dir.exists():
        return None
    for p in sorted(fetch_dir.glob(f"{stem}.*")):
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None


def _url_key(url):
    """Stable short stem fragment derived from the URL itself, so re-running the exact
    same URL finds the same file even before we know the remote title (used as a fallback
    prefix / dedup key alongside the title)."""
    m = re.search(r"(?:v=|/)([A-Za-z0-9_-]{6,})(?:[/?&]|$)", url)
    return sanitize_filename(m.group(1) if m else url, fallback="video")


# =============================================================================================
# yt-dlp path
# =============================================================================================

def _get_yt_dlp():
    """Lazy import: yt-dlp is optional. Returns the module, or None if it isn't installed."""
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return None
    return yt_dlp


def _ffmpeg_location(cfg):
    """Resolve an ffmpeg binary/dir for yt-dlp's merger (needed whenever it picks separate
    video+audio streams, i.e. most non-"already-progressive" formats). Same BS_FFMPEG env
    var + config.json "ffmpeg" key the rest of the pipeline uses; yt-dlp accepts either a
    directory or the exe path directly."""
    ffmpeg = os.environ.get("BS_FFMPEG") or (cfg or {}).get("ffmpeg")
    return ffmpeg or None


def fetch_with_ytdlp(url, output_base, fmt, force, cfg=None):
    yt_dlp = _get_yt_dlp()
    if yt_dlp is None:
        return None  # caller decides whether to fall back to plain download

    fetch_dir = Path(output_base) / FETCH_DIR_NAME
    fetch_dir.mkdir(parents=True, exist_ok=True)

    # Resolve the title first (no download) so we can dedup by the same filename a real
    # download would produce, without re-downloading to find out.
    probe_opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    title = None
    try:
        with yt_dlp.YoutubeDL(probe_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get("title") if info else None
    except Exception as e:
        print(f"LOG could not probe title ({e}); using URL as filename", flush=True)

    stem = sanitize_filename(title) if title else _url_key(url)

    if not force:
        existing = _existing_download(fetch_dir, stem)
        if existing is not None:
            return existing, True  # (path, was_skip)

    progress("fetch", 0, 1)

    def hook(d):
        if d.get("status") == "downloading":
            pct = (d.get("_percent_str") or "").strip()
            print(f"LOG downloading {pct} {d.get('_speed_str', '')}".rstrip(), flush=True)
        elif d.get("status") == "finished":
            print("LOG download finished, post-processing", flush=True)

    ydl_opts = {
        "outtmpl": str(fetch_dir / f"{stem}.%(ext)s"),
        # prefer a single progressive mp4 stream first (no ffmpeg merge needed); fall back
        # to separate best-video+best-audio mp4 (needs ffmpeg to mux) then anything.
        "format": fmt or "b[ext=mp4]/bv*[ext=mp4]+ba[ext=m4a]/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
        "overwrites": force,
    }
    ffmpeg_location = _ffmpeg_location(cfg)
    if ffmpeg_location:
        ydl_opts["ffmpeg_location"] = ffmpeg_location
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    progress("fetch", 1, 1)

    result = _existing_download(fetch_dir, stem)
    if result is None:
        raise SystemExit(f"bs_fetch: yt-dlp reported success but no output file matched "
                          f"'{stem}.*' under {fetch_dir}")
    return result, False


# =============================================================================================
# plain urllib fallback (direct media links only)
# =============================================================================================

def _looks_like_direct_media(url):
    path = url.split("?", 1)[0].split("#", 1)[0]
    return path.lower().endswith(DIRECT_MEDIA_EXTS)


def fetch_with_urllib(url, output_base, force):
    """Plain streaming download, no yt-dlp: only appropriate for a URL that already points
    straight at a media file (checked by extension, then confirmed by Content-Type)."""
    fetch_dir = Path(output_base) / FETCH_DIR_NAME
    fetch_dir.mkdir(parents=True, exist_ok=True)

    url_path = url.split("?", 1)[0].split("#", 1)[0]
    ext = Path(url_path).suffix or ".mp4"
    stem = sanitize_filename(Path(url_path).stem) or _url_key(url)
    dest = fetch_dir / f"{stem}{ext}"

    if not force and dest.exists() and dest.stat().st_size > 0:
        return dest, True

    progress("fetch", 0, 1)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (bs_fetch)"})
    with urllib.request.urlopen(req) as resp:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if not _looks_like_direct_media(url) and not ctype.startswith(("video/", "application/octet-stream")):
            raise SystemExit(
                f"bs_fetch: '{url}' does not look like a direct media link "
                f"(Content-Type: {ctype or 'unknown'}) and yt-dlp is not installed.\n"
                "Install yt-dlp to fetch from YouTube/Vimeo/etc.:\n"
                "    pip install yt-dlp"
            )
        total = int(resp.headers.get("Content-Length") or 0)
        tmp = dest.with_suffix(dest.suffix + ".part")
        done = 0
        chunk = 1024 * 256
        with open(tmp, "wb") as f:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                f.write(buf)
                done += len(buf)
                if total:
                    print(f"LOG downloading {done * 100 // total}% "
                          f"({done // (1024 * 1024)}MB/{total // (1024 * 1024)}MB)", flush=True)
        tmp.replace(dest)
    progress("fetch", 1, 1)
    return dest, False


# =============================================================================================
# entry point (also importable: fetch_url() is what both GUIs / bs_launcher call through)
# =============================================================================================

def fetch_url(url, output_base, cfg=None, fmt=None, force=False):
    """Fetch `url` into <output_base>/_fetched/, returning (Path, was_skip: bool).

    Tries yt-dlp first (handles YouTube/Vimeo/anything it knows); if yt-dlp isn't
    installed, falls back to a plain urllib download for direct http(s) links to a media
    file. Raises SystemExit with a friendly message otherwise.
    """
    cfg = cfg or {}
    if not is_url(url):
        raise SystemExit(f"bs_fetch: not a URL: '{url}'")

    result = fetch_with_ytdlp(url, output_base, fmt, force, cfg=cfg)
    if result is not None:
        return result

    print("LOG yt-dlp not installed; trying a direct download "
          "(only works for a URL that points straight at a video file)", flush=True)
    if not _looks_like_direct_media(url):
        raise SystemExit(
            "bs_fetch requires yt-dlp for this URL (not a direct link to a media file).\n"
            "Install it with:\n"
            "    pip install yt-dlp\n"
            "Then run again. (Direct https://.../file.mp4-style links work without yt-dlp.)"
        )
    return fetch_with_urllib(url, output_base, force)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="video URL: YouTube/Vimeo/etc. (needs yt-dlp) "
                                                  "or a direct http(s) link to a media file")
    ap.add_argument("--output-base", required=True, help="output_base root; the file lands in "
                                                          "<output-base>/_fetched/")
    ap.add_argument("--config", default="", help="path to config.json (reads 'ffmpeg' so yt-dlp "
                                                  "can mux separate video+audio streams)")
    ap.add_argument("--format", dest="fmt", default=None,
                     help="yt-dlp format selector override (default: best mp4 video+audio)")
    ap.add_argument("--force", action="store_true", help="re-download even if a matching file "
                                                          "already exists")
    args = ap.parse_args()

    cfg = load_config(args.config)
    url = args.url.strip()

    print(f"=== bs_fetch | url={url} | output_base={args.output_base} ===", flush=True)
    path, was_skip = fetch_url(url, args.output_base, cfg=cfg, fmt=args.fmt, force=args.force)
    abs_path = str(Path(path).resolve())

    if was_skip:
        print("LOG already downloaded, reusing existing file (use --force to re-fetch)", flush=True)
        print(f"SKIP {abs_path}", flush=True)
    else:
        print(f"FETCHED {abs_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
