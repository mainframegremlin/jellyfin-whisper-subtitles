#!/usr/bin/env python3
"""
subtitle_sync.py — Scan Jellyfin library, transcribe audio with Whisper,
and embed SRT subtitles into media files via ffmpeg.
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from faster_whisper import WhisperModel

load_dotenv()

JELLYFIN_URL = os.environ["JELLYFIN_URL"]
JELLYFIN_API_KEY = os.environ["JELLYFIN_API_KEY"]
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "en")
MEDIA_ROOT_REMOTE = os.getenv("MEDIA_ROOT_REMOTE", "")
MEDIA_ROOT_LOCAL = os.getenv("MEDIA_ROOT_LOCAL", "")
SUBTITLE_LOG = os.path.join(os.path.dirname(__file__), "subtitle_log.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Subtitle log helpers
# ---------------------------------------------------------------------------

def load_subtitle_log():
    if os.path.exists(SUBTITLE_LOG):
        with open(SUBTITLE_LOG) as f:
            return json.load(f)
    return {}


def save_subtitle_log(log_data):
    with open(SUBTITLE_LOG, "w") as f:
        json.dump(log_data, f, indent=2)


def was_processed(log_data, item_id):
    return item_id in log_data


def mark_processed(log_data, item_id):
    log_data[item_id] = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Jellyfin helpers
# ---------------------------------------------------------------------------

def jf_headers():
    return {
        "X-Emby-Token": JELLYFIN_API_KEY,
        "Content-Type": "application/json",
    }


def jf_get(path, **params):
    resp = requests.get(
        f"{JELLYFIN_URL}{path}",
        headers=jf_headers(),
        params=params,
        timeout=30,
    )
    if not resp.ok:
        log.error("  jf_get %s -> %s: %s", path, resp.status_code, resp.text[:300])
    resp.raise_for_status()
    return resp.json()


def get_user_id():
    users = jf_get("/Users")
    return users[0]["Id"]


def get_library_items(user_id):
    data = jf_get(
        f"/Users/{user_id}/Items",
        IncludeItemTypes="Movie,Episode",
        Recursive="true",
        Fields="Path,MediaStreams",
    )
    return data.get("Items", [])


def jellyfin_refresh_item(item_id):
    resp = requests.post(
        f"{JELLYFIN_URL}/Items/{item_id}/Refresh",
        headers=jf_headers(),
        params={
            "MetadataRefreshMode": "Default",
            "ImageRefreshMode": "Default",
            "ReplaceAllImages": "false",
            "ReplaceAllMetadata": "false",
        },
        timeout=30,
    )
    if not resp.ok:
        log.warning("  Jellyfin refresh failed (%s).", resp.status_code)


# ---------------------------------------------------------------------------
# Path remapping
# ---------------------------------------------------------------------------

def remote_to_local(path):
    """Remap a server-side path to the locally mounted equivalent."""
    if MEDIA_ROOT_REMOTE and MEDIA_ROOT_LOCAL and path.startswith(MEDIA_ROOT_REMOTE):
        return MEDIA_ROOT_LOCAL + path[len(MEDIA_ROOT_REMOTE):]
    return path


# ---------------------------------------------------------------------------
# Subtitle detection
# ---------------------------------------------------------------------------

def has_subtitle_stream(item):
    streams = item.get("MediaStreams") or []
    return any(s.get("Type") == "Subtitle" for s in streams)


# ---------------------------------------------------------------------------
# SRT generation
# ---------------------------------------------------------------------------

def _seconds_to_srt_timestamp(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def transcribe_to_srt(model, video_path, language):
    """Run Whisper on video_path and return an SRT string, or None if empty."""
    segments, _ = model.transcribe(video_path, language=language, beam_size=5)

    lines = []
    index = 1
    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        start = _seconds_to_srt_timestamp(seg.start)
        end = _seconds_to_srt_timestamp(seg.end)
        lines.append(f"{index}\n{start} --> {end}\n{text}\n")
        index += 1

    if not lines:
        return None
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ffmpeg embedding
# ---------------------------------------------------------------------------

EMBED_CONTAINERS = {".mkv", ".mp4", ".m4v"}
SUBTITLE_CODEC = {".mkv": "srt", ".mp4": "mov_text", ".m4v": "mov_text"}


def embed_subtitles(video_path, srt_path):
    """
    Mux srt_path into video_path as a soft subtitle track.
    Returns True on success, False on failure. Never modifies original on failure.
    """
    ext = os.path.splitext(video_path)[1].lower()
    if ext not in EMBED_CONTAINERS:
        return False

    codec = SUBTITLE_CODEC[ext]
    base = os.path.splitext(video_path)[0]
    tmp_path = base + ".whisper_tmp" + ext

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", srt_path,
        "-map", "0",
        "-map", "1",
        "-c", "copy",
        "-c:s", codec,
        "-metadata:s:s:0", "language=eng",
        "-metadata:s:s:0", "title=Whisper Auto",
        tmp_path,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log.error("  ffmpeg failed:\n%s", result.stderr[-800:])
            return False
        os.replace(tmp_path, video_path)
        return True
    except Exception as exc:
        log.error("  ffmpeg exception: %s", exc)
        return False
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def save_external_srt(video_path, srt_content):
    """Write a sidecar .en.srt file alongside the video. Returns True on success."""
    base = os.path.splitext(video_path)[0]
    srt_path = base + ".en.srt"
    try:
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(srt_content)
        return True
    except OSError as exc:
        log.error("  Cannot write external .srt: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Startup check
# ---------------------------------------------------------------------------

def check_dependencies():
    if not shutil.which("ffmpeg"):
        log.error("ffmpeg not found in PATH. Install it before running.")
        sys.exit(1)
    log.info("ffmpeg: %s", shutil.which("ffmpeg"))
    log.info("Whisper model: %s  Language: %s", WHISPER_MODEL, WHISPER_LANGUAGE)
    if MEDIA_ROOT_REMOTE and MEDIA_ROOT_LOCAL:
        log.info("Path remap: %s -> %s", MEDIA_ROOT_REMOTE, MEDIA_ROOT_LOCAL)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def process(dry_run, limit, force, model_override):
    check_dependencies()

    whisper_model_name = model_override or WHISPER_MODEL
    log.info("Loading Whisper model '%s' (this may take a moment)...", whisper_model_name)
    model = WhisperModel(whisper_model_name, device="auto", compute_type="auto")
    log.info("Whisper model loaded.")

    user_id = get_user_id()
    log.info("Jellyfin user ID: %s", user_id)
    items = get_library_items(user_id)
    log.info("Found %d items in library.", len(items))

    if limit:
        items = items[:limit]

    log_data = load_subtitle_log()
    ok = skipped = errors = 0

    for idx, item in enumerate(items, 1):
        item_id = item["Id"]
        title = item.get("Name", "<unknown>")
        kind = item.get("Type", "Movie")

        log.info("[%d/%d] %s (%s)", idx, len(items), title, kind)

        # Skip if already processed
        if not force and was_processed(log_data, item_id):
            log.info("  Already processed — skipping.")
            skipped += 1
            continue

        # Skip if subtitle streams already present
        if has_subtitle_stream(item):
            log.info("  Existing subtitle stream found — skipping.")
            mark_processed(log_data, item_id)
            save_subtitle_log(log_data)
            skipped += 1
            continue

        # Validate file path
        remote_path = item.get("Path")
        if not remote_path:
            log.warning("  No Path in item — skipping.")
            errors += 1
            continue

        video_path = remote_to_local(remote_path)
        if not os.path.isfile(video_path):
            log.warning("  File not found at %r — skipping.", video_path)
            errors += 1
            continue

        writable = os.access(video_path, os.W_OK)
        ext = os.path.splitext(video_path)[1].lower()
        can_embed = writable and ext in EMBED_CONTAINERS

        if dry_run:
            action = f"embed ({ext})" if can_embed else "external .srt"
            log.info("  [dry-run] would transcribe and %s", action)
            ok += 1
            continue

        # Transcribe
        log.info("  Transcribing %s...", os.path.basename(video_path))
        try:
            srt_content = transcribe_to_srt(model, video_path, WHISPER_LANGUAGE)
        except Exception as exc:
            log.error("  Whisper error: %s", exc)
            errors += 1
            continue

        if srt_content is None:
            log.warning("  Whisper returned empty transcription — skipping.")
            errors += 1
            continue

        log.info("  Transcription complete.")

        # Write temp SRT for ffmpeg
        srt_tmp = video_path + ".whisper.srt"
        success = False
        try:
            with open(srt_tmp, "w", encoding="utf-8") as f:
                f.write(srt_content)

            if can_embed:
                log.info("  Embedding subtitle track via ffmpeg...")
                success = embed_subtitles(video_path, srt_tmp)
                if success:
                    log.info("  Subtitle embedded successfully.")
                else:
                    log.warning("  ffmpeg failed; falling back to external .srt.")

            if not success:
                log.info("  Saving external .en.srt sidecar...")
                success = save_external_srt(video_path, srt_content)
                if success:
                    log.info("  External .srt saved.")

        finally:
            if os.path.exists(srt_tmp):
                os.unlink(srt_tmp)

        if not success:
            log.error("  Could not save subtitles — skipping mark.")
            errors += 1
            continue

        # Trigger Jellyfin rescan
        try:
            jellyfin_refresh_item(item_id)
            log.info("  Triggered Jellyfin library refresh.")
        except Exception as exc:
            log.warning("  Jellyfin refresh failed (non-fatal): %s", exc)

        mark_processed(log_data, item_id)
        save_subtitle_log(log_data)
        ok += 1

    log.info("Done. embedded/saved=%d  skipped=%d  errors=%d", ok, skipped, errors)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Transcribe Jellyfin media with Whisper and embed soft subtitles via ffmpeg."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without transcribing or modifying files.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Process only the first N items.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-process items already recorded in subtitle_log.json.",
    )
    parser.add_argument(
        "--model",
        default=None,
        metavar="MODEL",
        help="Whisper model to use (overrides WHISPER_MODEL env var).",
    )
    args = parser.parse_args()

    if args.dry_run:
        log.info("Dry-run mode — no files will be modified.")

    try:
        process(
            dry_run=args.dry_run,
            limit=args.limit,
            force=args.force,
            model_override=args.model,
        )
    except KeyboardInterrupt:
        log.info("Interrupted.")
        sys.exit(1)


if __name__ == "__main__":
    main()
