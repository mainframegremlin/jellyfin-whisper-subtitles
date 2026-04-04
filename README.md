# jellyfin-whisper-subtitles

Automatically generates and embeds soft subtitles for media in a Jellyfin library. Audio is transcribed with [faster-whisper](https://github.com/SYSTRAN/faster-whisper) and the result is embedded as a subtitle track via ffmpeg.

## Requirements

- Python 3.10+
- ffmpeg
- Jellyfin API key
- Media files accessible on the local filesystem

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env with your values
```

## Configuration


| Variable            | Description                                                                 |
| ------------------- | --------------------------------------------------------------------------- |
| `JELLYFIN_URL`      | Jellyfin server URL, e.g. `http://192.168.4.40:8096`                        |
| `JELLYFIN_API_KEY`  | API key from Jellyfin Dashboard → API Keys                                  |
| `WHISPER_MODEL`     | Model size: `tiny`, `base`, `small`, `medium`, `large-v3` (default: `base`) |
| `WHISPER_LANGUAGE`  | Language code for transcription (default: `en`)                             |
| `MEDIA_ROOT_REMOTE` | Path prefix Jellyfin uses on the server, e.g. `/volume1/media`              |
| `MEDIA_ROOT_LOCAL`  | Where that path is mounted locally, e.g. `/mnt/nas/media`                   |


If the script runs on the same machine as Jellyfin, leave `MEDIA_ROOT_REMOTE` and `MEDIA_ROOT_LOCAL` empty.

## Usage

```bash
# Dry run: show what would be processed, no files modified
python subtitle_sync.py --dry-run

# Process first 5 items (for testing)
python subtitle_sync.py --limit 5

# Full library run
python subtitle_sync.py

# Re-process already-logged items (e.g. to use a better model)
python subtitle_sync.py --force

# Override Whisper model without editing .env
python subtitle_sync.py --model small
```

## How It Works

1. Fetches all Movies and Episodes from Jellyfin
2. Skips items already recorded in `subtitle_log.json`
3. Skips items that already have subtitle streams
4. Remaps the Jellyfin server path to the local mount point
5. Transcribes audio with Whisper (reads video file directly)
6. For `.mkv`/`.mp4`/`.m4v`: embeds SRT as a soft subtitle track via ffmpeg, atomically replaces the original file
7. For other containers or read-only files: saves a `.en.srt` sidecar alongside the video
8. Triggers a Jellyfin library refresh so the track appears immediately
9. Records the item ID in `subtitle_log.json`

Progress is saved after each item, it is safe to interrupt and resume.

## Subtitle Embedding

Subtitles are soft (selectable, not burned in). The video and audio streams are stream-copied with no re-encoding.


| Container      | Subtitle codec             |
| -------------- | -------------------------- |
| `.mkv`         | `srt`                      |
| `.mp4`, `.m4v` | `mov_text`                 |
| Other          | external `.en.srt` sidecar |


## Model Selection


| Model      | Speed (CPU) | Notes                                                  |
| ---------- | ----------- | ------------------------------------------------------ |
| `tiny`     | Very fast   | Low accuracy, useful for quick passes                  |
| `base`     | Fast        | Good default for English content                       |
| `small`    | Moderate    | Better accuracy, recommended for mixed/accented speech |
| `medium`   | Slow        | High accuracy                                          |
| `large-v3` | Very slow   | Best accuracy, requires significant RAM                |


A 22-minute episode takes roughly 4 minutes with `base` on CPU.
