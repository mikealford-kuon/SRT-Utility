# Subtitle Workstation Architecture

Last updated: 2026-04-17 (WhisperX-first local execution with fallback enabled)

## Purpose

Subtitle Workstation is a local-first subtitle production app for large media files.
It is designed around a human-in-the-loop workflow:

1. ingest local media
2. generate or import transcript/subtitle content
3. edit subtitle segments in-app
4. export sidecar subtitle files
5. export MP4s with embedded soft subtitle tracks

The system is intentionally built so the editable source of truth is structured transcript data, not a raw `.srt` file.

## Current stack

### Backend
- FastAPI
- Pydantic models
- local JSON persistence at `backend/app/data/jobs.json`
- ffprobe for media inspection
- ffmpeg for embedded subtitle extraction and soft-subtitle MP4 export
- current local `whisper` CLI path for transcript generation
- WhisperX-first forced-alignment path with plain-Whisper fallback

### Frontend
- React + TypeScript
- local workstation UI for ingest, transcript editing, subtitle import, and export

## Canonical data model

### Job
A job represents one imported media asset and its subtitle-editing state.

Important fields include:
- `job_id`
- `media_path`
- `media_metadata`
- `stage`
- `transcription_mode`
- `transcription_source`
- `timing_source`
- `transcript_segments[]`
- `transcript_is_edited`
- `subtitle_tracks[]`

### Transcript segment
Each editable subtitle block is represented as a structured segment with:
- `segment_id`
- `start_seconds`
- `end_seconds`
- `text`
- `speaker`

The transcript segment list is the current editable truth inside the app.

### Stored subtitle track
Each job now owns a subtitle-track library.

Each stored track includes:
- `track_id`
- `source_kind` (`edited-transcript`, `uploaded-subtitle`, `sidecar-subtitle`, `embedded-subtitle`)
- `format_name`
- `language`
- `label`
- `subtitle_path`
- `is_default`
- `is_active`
- `origin_note`
- `created_at`
- `updated_at`

The edited transcript is automatically represented as a first-class stored subtitle track for export.
Imported tracks are persisted into the job’s track library so the export path is no longer dependent on one-off manual file arguments.

## Synchronization architecture

### Current timing behavior
The current fallback transcription path uses plain Whisper CLI output and then maps text into subtitle segments.
That remains acceptable as an emergency fallback, but it is not the target-quality timing architecture for long-form subtitle sync.
The backend now executes WhisperX first whenever it can find a runnable WhisperX command, with the canonical local setup expecting WhisperX to be present in the backend virtualenv from `backend/requirements.txt`.
This WhisperX-first path has now been verified end to end on local spoken media.
The backend stores timing provenance to distinguish:
- plain Whisper timing (`plain-whisper-cli-estimated`)
- Whisper fallback after WhisperX issues (`whisperx-fallback:whisper-cli-estimated|...`)
- WhisperX aligned timing (`real-cli:whisperx-aligned`)

Observed failure mode:
- subtitles may begin reasonably close to correct timing
- then drift or fall progressively out of sync later in the video
- global shift can repair constant offset, but not progressive timing drift

### Adopted design change
The workstation has now moved from raw Whisper timing toward a two-stage synchronization architecture:

1. Whisper (or equivalent ASR) for transcript text
2. WhisperX forced alignment for word/segment timing refinement

This is now the implemented default synchronization design for the product, with plain Whisper retained only as an emergency fallback.

### Why WhisperX
Whisper alone is strongest at transcript generation, not long-form timestamp precision.
WhisperX adds a forced-alignment stage intended to improve timestamp accuracy materially, especially for subtitle timing.

### Product implication
Future automatic subtitle generation should prefer:
- transcript text from Whisper
- timing from WhisperX alignment
- subtitle segmentation/export after alignment

Global timing shift remains useful, but should be treated as a repair tool, not the primary synchronization method.

## Import architecture

### Supported current inputs
1. direct media ingest by local path
2. direct media upload
3. edited `.srt` / `.vtt` applied against the current job timing
4. sidecar subtitle text found next to media, applied against the current job timing
5. embedded subtitle stream text, applied against the current job timing

### Subtitle source discovery behavior
For video jobs, the app now exposes multiple subtitle-source options in the editor:
- manual SRT/VTT edit carry-forward
- auto-detected sidecar subtitle text when a matching `.srt` or `.vtt` sits next to the source video
- embedded subtitle-track text when ffprobe finds subtitle streams in the container

When no sidecar or embedded subtitle source exists, the UI now says so explicitly instead of silently presenting an empty apply path.

### Correction carry-forward
The workstation now treats an uploaded edited subtitle file as a correction source for a changed video:

1. ingest or process the changed video so the current job has fresh timing
2. upload the previous corrected `.srt` or `.vtt`
3. align old subtitle text to the current job's subtitle sequence by normalized text similarity
4. keep `start_seconds` / `end_seconds` from the current job
5. copy corrected `text` / `speaker` from the previous subtitle where confidence is high
6. infer repeated correction patterns such as names, product terms, casing, and joined-word fixes
7. apply learned corrections to genuinely new material when confidence is high
8. flag likely repeated mistakes as sore thumbs when confidence is uncertain

The backend endpoint is `POST /jobs/{job_id}/retime-edited-subtitles`.

The operation preserves a recoverable pre-retime snapshot and stores the uploaded previous edit file in the job's subtitle-track library. Returned transcript segments include optional `retime_confidence`, `retime_status`, `retime_note`, and `correction_suggestions` fields so the UI can filter low-confidence rows, auto-corrections, and sore thumbs for review.

### Media probing
- `ffprobe` determines file metadata and stream availability
- video jobs are required for embedded subtitle extraction and MP4 soft-sub export

## Export architecture

### Sidecar export
The app currently exports:
- `SRT`
- `VTT`

These are rendered from canonical transcript segments.

### Soft-subtitle MP4 export
The app now exports an MP4 with one or more embedded soft subtitle tracks by:
1. rendering the current edited transcript to temporary SRT for the primary track
2. loading additional `.srt` or `.vtt` files for extra language tracks
3. converting `.vtt` inputs to SRT internally when needed
4. muxing the source video and all subtitle tracks through `ffmpeg`
5. encoding subtitle streams as `mov_text`
6. copying video/audio streams where possible
7. writing per-track subtitle metadata and disposition flags

Current ffmpeg behavior per subtitle stream:
- subtitle codec: `mov_text`
- stream metadata `language=<code>`
- stream metadata `title=<label>`
- optional `default` disposition on selected subtitle streams

This fixes the prior behavior where exported MP4 subtitle tracks could appear in players as `Unknown language`, and now supports true multi-track embedded subtitle export.

## Subtitle track metadata model

The export API now supports explicit multi-track subtitle metadata.

### Track model
The export request accepts `tracks[]`.

Each track contains:
- `language` example: `eng`, `spa`
- `label` example: `English Subtitles`, `Spanish Subtitles`
- `subtitle_path` optional filesystem path to `.srt` or `.vtt`
- `is_default` boolean default-track flag

### Primary track behavior
- The first track may omit `subtitle_path`
- when omitted, the app uses the current edited transcript segments as the subtitle source

### Additional track behavior
- extra tracks come from stored subtitle-track assets owned by the job
- supported formats are `.srt` and `.vtt`
- `.vtt` tracks are parsed and normalized into SRT before muxing
- manual filesystem paths are ingested into stored track assets first, then exported from the library

This is now true multi-track muxing with an in-app stored track library.

## UI architecture

### Current workstation panels
- Media Import
- Jobs
- Waveform / Editor placeholder
- Export

### Timing correction workflow
The primary transcript editor now includes a global timing shift control.

Behavior:
- positive shift values move all subtitles later
- negative shift values move all subtitles earlier
- default draft shift value is `+5.0` seconds
- shift is applied directly to the editable transcript draft before save/export
- start times clamp at `0`
- segment duration is preserved if a shift would otherwise collapse a segment

This is the intended first-line repair tool for globally early/late subtitle timing, and is much safer than dragging each segment manually.

### Export panel behavior
The export UI now includes:
- output directory
- output filename
- backend compatibility for either a fully resolved `output_path` or an `output_dir` + `output_filename` pair
- visible stored subtitle-track library for the selected job
- draft rows for creating new stored tracks from `.srt` / `.vtt` files
- per-track remove actions for non-primary stored tracks

Export now uses the job’s stored active subtitle tracks instead of ad hoc path entry at export time.
The edited in-app transcript is treated as the primary export source and is forced to the front of the export track list so timing/text changes made in the editor affect the muxed MP4.
This avoids the failure mode where a stale stored subtitle track could be exported while the edited transcript looked correct on screen.

This is the current canonical workflow for multilingual MP4 export.

## Persistence and recovery

### Live data
- job state persists in `backend/app/data/jobs.json`

### Backups
Recoverable app snapshots should be stored under:
- `projects/subtitle-workstation/backups/<timestamp>/`

A backup snapshot should include at least:
- `backend/`
- `frontend/`

Latest confirmed snapshot for this architecture state:
- `projects/subtitle-workstation/backups/2026-04-17-002637/`

## Current limitations

1. No database yet, local JSON only
2. No auth/user model yet
3. No per-track inline editing yet for non-primary stored subtitle tracks
4. No hard-burn pipeline in current workflow
5. WhisperX execution still depends on the backend virtualenv installing cleanly on the local machine, but the canonical setup now pins it in `backend/requirements.txt`
6. No waveform editor integration yet
7. Sidecar auto-detect currently uses simple same-basename matching only
8. Multi-sidecar selection UX is not implemented yet
9. Embedded subtitle extraction depends on ffmpeg/ffprobe-supported subtitle codecs in the source container
10. Current plain-Whisper timing path can still drift over long videos

## Recommended next architectural step

Harden failure handling and re-sync workflows around the now-working WhisperX-first path.

Suggested implementation order:
- keep the backend virtualenv healthy and reproducible from pinned requirements
- preserve plain-Whisper as an emergency fallback only
- continue storing whether a transcript was timed by raw Whisper or WhisperX
- keep alignment provenance visible in the UI
- resolve non-speech and empty-output jobs with especially clear user-facing status
- later add a dedicated “re-sync existing subtitles to media” mode for correcting already-good text with bad timing

The architecture is now strong on subtitle-source, export workflow, and default automatic synchronization for spoken media. The main remaining structural gap is polish around edge cases and re-sync tooling.
