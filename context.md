# Project Context

## Background

This project was built to automatically generate and attach subtitles to media in a Jellyfin library. The goal was a zero-touch pipeline: for any media item that lacks subtitle tracks, transcribe the audio with Whisper and embed the result as a soft subtitle track via ffmpeg.

## Environment

- **Script machine:** ThinkCentre, MX Linux
- **Jellyfin server:** Synology NAS, also hosts the media files
- **Media share:** `/volume1/media` on the NAS, mounted at `/mnt/nas/media` on the mx box
- **Mount convention:** `/mnt/nas/<sharename>` via SMB/CIFS
- **Credentials file:** `/etc/nas-credentials` (shared with other NAS mounts)

## Design Decisions

**Why faster-whisper over openai-whisper:** Significantly faster on CPU, same model weights, same quality. Uses CTranslate2 under the hood. `device="auto"` selects CUDA if available, CPU otherwise.

**Why faster-whisper reads video directly:** CTranslate2 invokes ffmpeg internally to decode audio from the video file. No intermediate audio extraction step needed. This simplifies the pipeline and avoids temp file management for the audio.

**Why soft subtitles via ffmpeg (not sidecar .srt):** Embedding the subtitle track in the container means a single file to manage. Jellyfin picks it up immediately after a metadata refresh. Sidecar files are used as a fallback only when the container is unsupported or the file is read-only.

**Why the temp-file-then-atomic-rename pattern:** The original file is never touched while ffmpeg is running. If ffmpeg fails (disk full, bad input, etc.), the original is intact. `os.replace()` maps to `rename(2)` on Linux, atomic at the kernel level. The temp file is created in the same directory as the original to guarantee they share the same filesystem.

**Why per-item log saves:** `subtitle_log.json` is written after each successfully processed item rather than once at the end. A keyboard interrupt or crash mid-run doesn't lose all progress. The next run picks up where it left off.

**Why items with existing subtitles are marked in the log:** Prevents re-checking `MediaStreams` on every run. Once an item is confirmed to have subtitles (whether pre-existing or freshly embedded), it's recorded and skipped permanently.

**Why** `IncludeItemTypes=Movie,Episode` **(not Series):** Series and Season items are containers—they have no `Path` and no media file. Only Movie and Episode items have actual video files. Fetching Series would just produce warning logs for every show.

**Why path remapping via env vars:** The script runs on the mx box but Jellyfin runs on the NAS. Jellyfin returns paths as they appear on the NAS (`/volume1/media/...`). `MEDIA_ROOT_REMOTE` and `MEDIA_ROOT_LOCAL` translate these to the locally mounted equivalent without hardcoding paths in the script.

**Subtitle codec per container:**

- `.mkv` → `srt` (Matroska natively supports SRT streams)
- `.mp4` / `.m4v` → `mov_text` (MP4's required soft subtitle format)
- Everything else → external `.en.srt` sidecar (Jellyfin auto-detects these)

## SMB Mount for Media Share

Same options as the pictures share in fstab:

```
//NAS_IP/media  /mnt/nas/media  cifs  credentials=/etc/nas-credentials,uid=1000,gid=1000,iocharset=utf8,vers=3.0,_netdev,noauto,x-systemd.automount,x-systemd.idle-timeout=60  0  0
```

## Scheduling

Runs via cron at 3am daily. Logs append to `/var/log/jellyfin-whisper-subtitles/subtitle_sync.log`.

```
0 3 * * * /home/USER/code/jellyfin-whisper-subtitles/.venv/bin/python /home/grem/code/jellyfin-whisper-subtitles/subtitle_sync.py >> /var/log/jellyfin-whisper-subtitles/subtitle_sync.log 2>&1
```

Cron was chosen over event-driven triggering (Jellyfin webhooks) because transcription is slow and better run during idle hours. The script exits quickly when there is nothing new to process.

## Transcription Performance (base model, CPU)

- ~22-minute episode → ~4 minutes transcription time
- Scales roughly linearly with audio duration
- Upgrade to `small` or `medium` model for better accuracy on difficult audio

## File Structure

```
jellyfin-whisper-subtitles/
├── subtitle_sync.py       # main script
├── .env.example           # config template
├── .env                   # credentials (not in repo)
├── requirements.txt       # faster-whisper, requests, python-dotenv
├── .gitignore
├── context.md             # this file
├── README.md              # usage docs
└── subtitle_log.json      # auto-created; tracks processed item IDs
```

