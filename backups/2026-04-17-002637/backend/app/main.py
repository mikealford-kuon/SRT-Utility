from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="Subtitle Workstation API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class IngestRequest(BaseModel):
    media_path: str = Field(..., description="Local media file path")


class IngestResponse(BaseModel):
    job_id: str
    status: Literal["queued"]
    message: str


class MediaMetadata(BaseModel):
    file_name: str
    size_bytes: int = Field(..., ge=0)
    duration_seconds: float | None = Field(default=None, ge=0)
    has_video: bool | None = None
    has_audio: bool | None = None


JobStage = Literal["queued", "transcribing", "aligned", "diarized", "ready"]

STAGE_ORDER: tuple[JobStage, ...] = (
    "queued",
    "transcribing",
    "aligned",
    "diarized",
    "ready",
)

STAGE_PROGRESS: dict[JobStage, int] = {
    "queued": 0,
    "transcribing": 25,
    "aligned": 55,
    "diarized": 80,
    "ready": 100,
}


class JobSummary(BaseModel):
    job_id: str
    kind: str
    media_path: str
    media_metadata: MediaMetadata | None = None
    transcription_mode: Literal["placeholder", "real-cli"] = "placeholder"
    transcription_source: str = "placeholder-synthetic"
    stage: JobStage
    progress_percent: int = Field(..., ge=0, le=100)
    created_at: str
    updated_at: str


class TranscriptSegment(BaseModel):
    segment_id: str
    start_seconds: float = Field(..., ge=0)
    end_seconds: float = Field(..., gt=0)
    text: str
    speaker: str | None = None


class StoredSubtitleTrack(BaseModel):
    track_id: str
    source_kind: Literal["edited-transcript", "uploaded-subtitle", "sidecar-subtitle", "embedded-subtitle"]
    format_name: Literal["srt", "vtt"]
    language: str = "eng"
    label: str = "English Subtitles"
    subtitle_path: str | None = None
    is_default: bool = False
    is_active: bool = True
    origin_note: str | None = None
    created_at: str
    updated_at: str


class JobDetail(JobSummary):
    transcript_segments: list[TranscriptSegment] = Field(default_factory=list)
    transcript_is_edited: bool = False
    subtitle_tracks: list[StoredSubtitleTrack] = Field(default_factory=list)


class UpdateTranscriptRequest(BaseModel):
    transcript_segments: list[TranscriptSegment] = Field(default_factory=list)


class SubtitleTrackMetadata(BaseModel):
    language: str = Field(default="eng", description="BCP-47/ISO 639 language tag, e.g. eng, spa")
    label: str = Field(default="English Subtitles", description="Human-readable subtitle track label")
    subtitle_path: str | None = Field(
        default=None,
        description="Optional external .srt/.vtt file path for this track. Omit to use the current edited transcript for the primary track.",
    )
    is_default: bool = Field(default=False, description="Whether this subtitle track should be marked as default")


class ExportSoftSubtitleMp4Request(BaseModel):
    output_path: str = Field(..., description="Destination MP4 path")
    track_ids: list[str] = Field(
        default_factory=list,
        description="Stored subtitle track ids to mux into the MP4. Empty means export active tracks.",
    )


class CreateSubtitleTrackRequest(BaseModel):
    language: str = "eng"
    label: str = "English Subtitles"
    subtitle_path: str = Field(..., description="Path to a local .srt or .vtt file")
    is_default: bool = False


class ImportEmbeddedSubtitleTrackRequest(BaseModel):
    stream_index: int | None = Field(default=None, description="Subtitle stream index")


jobs: list[JobDetail] = []
next_job_id = 1
DATA_DIR = Path(__file__).parent / "data"
UPLOADS_DIR = DATA_DIR / "uploads"
STORE_PATH = DATA_DIR / "jobs.json"


def save_state() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "next_job_id": next_job_id,
        "jobs": [job.model_dump() for job in jobs],
    }
    STORE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def summarize_job(job: JobDetail) -> JobSummary:
    return JobSummary.model_validate(job.model_dump())


def make_track_id(job_id: str, index: int) -> str:
    return f"{job_id}-track-{index:03d}"


def ensure_default_edited_track(job: JobDetail) -> JobDetail:
    if any(track.source_kind == "edited-transcript" for track in job.subtitle_tracks):
        return job
    now = datetime.now(timezone.utc).isoformat()
    edited_track = StoredSubtitleTrack(
        track_id=make_track_id(job.job_id, 1),
        source_kind="edited-transcript",
        format_name="srt",
        language="eng",
        label="English Subtitles",
        subtitle_path=None,
        is_default=True,
        is_active=True,
        origin_note="Current edited transcript",
        created_at=now,
        updated_at=now,
    )
    return job.model_copy(update={"subtitle_tracks": [edited_track, *job.subtitle_tracks]})


def renumber_tracks(job: JobDetail) -> JobDetail:
    updated_tracks: list[StoredSubtitleTrack] = []
    for index, track in enumerate(job.subtitle_tracks, start=1):
        updated_tracks.append(
            track.model_copy(update={"track_id": make_track_id(job.job_id, index)})
        )
    return job.model_copy(update={"subtitle_tracks": updated_tracks})


def assign_default_track(job: JobDetail, target_track_id: str | None) -> JobDetail:
    updated_tracks: list[StoredSubtitleTrack] = []
    for track in job.subtitle_tracks:
        should_default = track.track_id == target_track_id if target_track_id else False
        updated_tracks.append(
            track.model_copy(
                update={
                    "is_default": should_default,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        )
    return job.model_copy(update={"subtitle_tracks": updated_tracks})


def create_stored_track(
    *,
    job: JobDetail,
    source_kind: Literal["edited-transcript", "uploaded-subtitle", "sidecar-subtitle", "embedded-subtitle"],
    format_name: Literal["srt", "vtt"],
    subtitle_path: str | None,
    label: str,
    language: str,
    origin_note: str | None,
    is_default: bool,
) -> StoredSubtitleTrack:
    now = datetime.now(timezone.utc).isoformat()
    return StoredSubtitleTrack(
        track_id=make_track_id(job.job_id, len(job.subtitle_tracks) + 1),
        source_kind=source_kind,
        format_name=format_name,
        language=language.strip() or "eng",
        label=label.strip() or "Subtitle Track",
        subtitle_path=subtitle_path,
        is_default=is_default,
        is_active=True,
        origin_note=origin_note,
        created_at=now,
        updated_at=now,
    )


def load_state() -> None:
    global jobs, next_job_id
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not STORE_PATH.exists():
        jobs = []
        next_job_id = 1
        save_state()
        return

    try:
        raw_state = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        raw_jobs = raw_state.get("jobs", [])
        jobs = [JobDetail.model_validate(job) for job in raw_jobs]
        did_backfill_segments = False
        for index, job in enumerate(jobs):
            next_job = job
            if not next_job.transcript_segments:
                next_job = next_job.model_copy(
                    update={
                        "transcript_segments": build_placeholder_segments(
                            stage=next_job.stage,
                            job_id=next_job.job_id,
                            media_metadata=next_job.media_metadata,
                        )
                    }
                )
                did_backfill_segments = True
            ensured_job = renumber_tracks(ensure_default_edited_track(next_job))
            if ensured_job != job:
                jobs[index] = ensured_job
                did_backfill_segments = True
        next_job_id = int(raw_state.get("next_job_id", len(jobs) + 1))
        if did_backfill_segments:
            save_state()
    except (json.JSONDecodeError, ValueError, TypeError):
        jobs = []
        next_job_id = 1
        save_state()


@app.on_event("startup")
def on_startup() -> None:
    load_state()


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "subtitle-workstation-api",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def probe_media(media_path: Path) -> dict:
    ffprobe_binary = shutil.which("ffprobe")
    if ffprobe_binary is None:
        raise HTTPException(status_code=400, detail="ffprobe is not installed.")

    try:
        probe_result = subprocess.run(
            [
                ffprobe_binary,
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(media_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(
            status_code=400,
            detail=f"ffprobe timed out while inspecting '{media_path}': {exc}",
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to execute ffprobe for '{media_path}': {exc}",
        ) from exc

    if probe_result.returncode != 0:
        stderr_message = (probe_result.stderr or "").strip()
        detail = stderr_message if stderr_message else "Unknown ffprobe error."
        raise HTTPException(
            status_code=400,
            detail=f"ffprobe could not read media file '{media_path}': {detail}",
        )

    try:
        return json.loads(probe_result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"ffprobe returned invalid JSON for '{media_path}'.",
        ) from exc


def build_media_metadata(media_path: Path) -> MediaMetadata:
    metadata = MediaMetadata(
        file_name=media_path.name,
        size_bytes=media_path.stat().st_size,
    )
    try:
        parsed = probe_media(media_path)
    except HTTPException as exc:
        if exc.detail == "ffprobe is not installed.":
            return metadata
        raise

    streams = parsed.get("streams")
    if not isinstance(streams, list):
        streams = []
    has_video = any(stream.get("codec_type") == "video" for stream in streams)
    has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
    if not streams or (not has_video and not has_audio):
        raise HTTPException(
            status_code=400,
            detail=f"No audio/video streams found in '{media_path}'.",
        )

    duration_seconds: float | None = None
    format_data = parsed.get("format")
    if isinstance(format_data, dict):
        duration_raw = format_data.get("duration")
        if duration_raw is not None:
            try:
                duration_candidate = float(duration_raw)
                if duration_candidate >= 0:
                    duration_seconds = duration_candidate
            except (TypeError, ValueError):
                duration_seconds = None

    return metadata.model_copy(
        update={
            "duration_seconds": duration_seconds,
            "has_video": has_video,
            "has_audio": has_audio,
        }
    )


def build_placeholder_segments(
    *,
    stage: JobStage,
    job_id: str,
    media_metadata: MediaMetadata | None,
) -> list[TranscriptSegment]:
    duration_seconds = media_metadata.duration_seconds if media_metadata else None
    total_duration = duration_seconds if duration_seconds and duration_seconds > 0 else 18.0
    segment_count = 5
    spacing = total_duration / (segment_count + 0.5)
    stage_blurb = {
        "queued": "Queued placeholder segment",
        "transcribing": "Draft transcript candidate",
        "aligned": "Aligned subtitle draft",
        "diarized": "Speaker-tagged subtitle draft",
        "ready": "Ready-to-export subtitle line",
    }[stage]
    include_speaker = stage in {"diarized", "ready"}

    segments: list[TranscriptSegment] = []
    for index in range(segment_count):
        start_seconds = round(index * spacing, 2)
        end_seconds = round(start_seconds + max(spacing * 0.68, 1.2), 2)
        if end_seconds <= start_seconds:
            end_seconds = round(start_seconds + 1.2, 2)
        segments.append(
            TranscriptSegment(
                segment_id=f"{job_id}-seg-{index + 1:03d}",
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                text=f"{stage_blurb} {index + 1}.",
                speaker=f"SPK_{(index % 2) + 1:02d}" if include_speaker else None,
            )
        )
    return segments


def split_text_into_segments(
    *,
    text: str,
    job_id: str,
    total_duration_seconds: float | None,
) -> list[TranscriptSegment]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        words = [token for token in text.replace("\n", " ").split(" ") if token.strip()]
        if words:
            chunk_size = 14
            lines = [
                " ".join(words[index : index + chunk_size])
                for index in range(0, len(words), chunk_size)
            ]

    if not lines:
        return []

    total_duration = (
        total_duration_seconds if total_duration_seconds and total_duration_seconds > 0 else 4.0
    )
    spacing = max(total_duration / len(lines), 1.2)
    segments: list[TranscriptSegment] = []
    for index, line in enumerate(lines):
        start_seconds = round(index * spacing, 2)
        end_seconds = round(start_seconds + spacing * 0.9, 2)
        if end_seconds <= start_seconds:
            end_seconds = round(start_seconds + 1.0, 2)
        segments.append(
            TranscriptSegment(
                segment_id=f"{job_id}-seg-{index + 1:03d}",
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                text=line,
            )
        )
    return segments


def format_srt_timestamp(total_seconds: float) -> str:
    safe_seconds = max(0.0, total_seconds)
    millis_total = int(round(safe_seconds * 1000))
    hours = millis_total // 3_600_000
    minutes = (millis_total % 3_600_000) // 60_000
    seconds = (millis_total % 60_000) // 1000
    millis = millis_total % 1000
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def format_vtt_timestamp(total_seconds: float) -> str:
    safe_seconds = max(0.0, total_seconds)
    millis_total = int(round(safe_seconds * 1000))
    hours = millis_total // 3_600_000
    minutes = (millis_total % 3_600_000) // 60_000
    seconds = (millis_total % 60_000) // 1000
    millis = millis_total % 1000
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def normalize_segment_bounds(segment: TranscriptSegment) -> tuple[float, float]:
    start = max(0.0, float(segment.start_seconds))
    end = max(0.0, float(segment.end_seconds))
    if end <= start:
        end = start + 0.01
    return start, end


def render_srt(segments: list[TranscriptSegment]) -> str:
    lines: list[str] = []
    for index, segment in enumerate(segments, start=1):
        start, end = normalize_segment_bounds(segment)
        text_value = segment.text.strip() if segment.text.strip() else "..."
        lines.extend(
            [
                str(index),
                f"{format_srt_timestamp(start)} --> {format_srt_timestamp(end)}",
                text_value,
                "",
            ]
        )
    return "\n".join(lines).strip() + ("\n" if lines else "")


def render_vtt(segments: list[TranscriptSegment]) -> str:
    lines: list[str] = ["WEBVTT", ""]
    for segment in segments:
        start, end = normalize_segment_bounds(segment)
        text_value = segment.text.strip() if segment.text.strip() else "..."
        lines.extend(
            [
                f"{format_vtt_timestamp(start)} --> {format_vtt_timestamp(end)}",
                text_value,
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def parse_srt_timestamp(raw_value: str) -> float:
    parts = raw_value.strip().replace(",", ".").split(":")
    if len(parts) != 3:
        raise ValueError(f"Invalid SRT timestamp: {raw_value}")
    hours = int(parts[0])
    minutes = int(parts[1])
    seconds = float(parts[2])
    return hours * 3600 + minutes * 60 + seconds


def parse_vtt_timestamp(raw_value: str) -> float:
    parts = raw_value.strip().split(":")
    if len(parts) == 2:
        hours = 0
        minutes = int(parts[0])
        seconds = float(parts[1])
        return hours * 3600 + minutes * 60 + seconds
    if len(parts) == 3:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = float(parts[2])
        return hours * 3600 + minutes * 60 + seconds
    raise ValueError(f"Invalid VTT timestamp: {raw_value}")


def parse_subtitle_text_to_segments(
    *, content: str, format_name: Literal["srt", "vtt"], job_id: str
) -> list[TranscriptSegment]:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="Subtitle file is empty.")

    blocks = [block.strip() for block in normalized.split("\n\n") if block.strip()]
    segments: list[TranscriptSegment] = []
    segment_index = 1

    for block in blocks:
        lines = [line.strip("\ufeff") for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        if format_name == "vtt" and lines[0].upper() == "WEBVTT":
            continue

        if "-->" in lines[0]:
            timing_line = lines[0]
            text_lines = lines[1:]
        elif len(lines) >= 2 and "-->" in lines[1]:
            timing_line = lines[1]
            text_lines = lines[2:]
        else:
            continue

        start_raw, end_raw = [part.strip() for part in timing_line.split("-->", maxsplit=1)]
        start_seconds = (
            parse_srt_timestamp(start_raw) if format_name == "srt" else parse_vtt_timestamp(start_raw)
        )
        end_seconds = (
            parse_srt_timestamp(end_raw) if format_name == "srt" else parse_vtt_timestamp(end_raw)
        )
        if end_seconds <= start_seconds:
            end_seconds = start_seconds + 0.01

        text_value = "\n".join(text_lines).strip() if text_lines else "..."
        segments.append(
            TranscriptSegment(
                segment_id=f"{job_id}-seg-{segment_index:03d}",
                start_seconds=round(start_seconds, 3),
                end_seconds=round(end_seconds, 3),
                text=text_value,
                speaker=None,
            )
        )
        segment_index += 1

    if not segments:
        raise HTTPException(
            status_code=400,
            detail=f"No valid subtitle segments found in {format_name.upper()} file.",
        )
    return segments


def find_sidecar_subtitle_candidates(media_path: Path) -> list[dict]:
    base_without_suffix = media_path.with_suffix("")
    candidates: list[dict] = []
    for extension in (".srt", ".vtt"):
        candidate_path = Path(f"{base_without_suffix}{extension}")
        if candidate_path.exists() and candidate_path.is_file():
            candidates.append(
                {
                    "path": str(candidate_path),
                    "format": extension.removeprefix("."),
                    "file_name": candidate_path.name,
                }
            )
    return candidates


def import_sidecar_subtitle_path(*, job: JobDetail, subtitle_path: Path) -> tuple[list[TranscriptSegment], str]:
    suffix = subtitle_path.suffix.lower()
    if suffix not in {".srt", ".vtt"}:
        raise HTTPException(status_code=400, detail="Sidecar subtitle must be .srt or .vtt")
    if not subtitle_path.exists() or not subtitle_path.is_file():
        raise HTTPException(status_code=400, detail=f"Sidecar subtitle not found: {subtitle_path}")

    try:
        content = subtitle_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Sidecar subtitle file must be UTF-8 encoded.") from exc

    format_name: Literal["srt", "vtt"] = "srt" if suffix == ".srt" else "vtt"
    segments = parse_subtitle_text_to_segments(
        content=content,
        format_name=format_name,
        job_id=job.job_id,
    )
    return segments, f"sidecar-{format_name}"


def list_subtitle_streams(media_path: Path) -> list[dict]:
    parsed = probe_media(media_path)
    streams = parsed.get("streams")
    if not isinstance(streams, list):
        return []

    subtitle_streams: list[dict] = []
    for stream in streams:
        if stream.get("codec_type") != "subtitle":
            continue
        tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
        subtitle_streams.append(
            {
                "index": stream.get("index"),
                "codec_name": stream.get("codec_name"),
                "language": tags.get("language"),
                "title": tags.get("title"),
            }
        )
    return subtitle_streams


def extract_embedded_subtitle_segments(
    *, job: JobDetail, stream_index: int | None
) -> tuple[list[TranscriptSegment], str]:
    ffmpeg_binary = shutil.which("ffmpeg")
    if ffmpeg_binary is None:
        raise HTTPException(status_code=400, detail="ffmpeg is not installed.")
    if not job.media_metadata or job.media_metadata.has_video is not True:
        raise HTTPException(
            status_code=400,
            detail="Embedded subtitle extraction requires a video job.",
        )

    source_path = Path(job.media_path)
    if not source_path.exists() or not source_path.is_file():
        raise HTTPException(
            status_code=400,
            detail=f"Source media is missing: {source_path}",
        )

    subtitle_streams = list_subtitle_streams(source_path)
    if not subtitle_streams:
        raise HTTPException(status_code=400, detail="No embedded subtitle tracks found.")

    chosen_stream = None
    if stream_index is None:
        chosen_stream = subtitle_streams[0]
    else:
        for stream in subtitle_streams:
            if stream.get("index") == stream_index:
                chosen_stream = stream
                break
        if chosen_stream is None:
            raise HTTPException(
                status_code=400,
                detail=f"Subtitle stream index not found: {stream_index}",
            )

    with tempfile.TemporaryDirectory(prefix="subtitle-embedded-") as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        extracted_path = temp_dir / f"{job.job_id}.vtt"
        command = [
            ffmpeg_binary,
            "-y",
            "-i",
            str(source_path),
            "-map",
            f"0:{chosen_stream['index']}",
            str(extracted_path),
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=600,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Failed to extract embedded subtitles: {exc}",
            ) from exc

        if result.returncode != 0 or not extracted_path.exists():
            stderr_message = (result.stderr or "").strip()
            detail = stderr_message if stderr_message else "Unknown ffmpeg error."
            raise HTTPException(
                status_code=400,
                detail=f"Embedded subtitle extraction failed: {detail}",
            )

        content = extracted_path.read_text(encoding="utf-8-sig")
        segments = parse_subtitle_text_to_segments(
            content=content,
            format_name="vtt",
            job_id=job.job_id,
        )
        return segments, f"embedded-stream-{chosen_stream['index']}"


def ensure_softsub_export_target(output_path_raw: str) -> Path:
    normalized_path = Path(output_path_raw.strip()).expanduser()
    if not output_path_raw.strip():
        raise HTTPException(status_code=400, detail="output_path is required.")
    if normalized_path.suffix.lower() != ".mp4":
        raise HTTPException(status_code=400, detail="output_path must end with .mp4")

    parent_dir = normalized_path.parent
    if not parent_dir.exists():
        raise HTTPException(
            status_code=400,
            detail=f"Output directory does not exist: {parent_dir}",
        )
    if not parent_dir.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"Output parent is not a directory: {parent_dir}",
        )
    return normalized_path


def load_subtitle_text_from_path(subtitle_path: Path) -> tuple[str, Literal["srt", "vtt"]]:
    suffix = subtitle_path.suffix.lower()
    if suffix not in {".srt", ".vtt"}:
        raise HTTPException(status_code=400, detail=f"Unsupported subtitle format: {subtitle_path.name}")
    if not subtitle_path.exists() or not subtitle_path.is_file():
        raise HTTPException(status_code=400, detail=f"Subtitle file not found: {subtitle_path}")
    try:
        content = subtitle_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Subtitle file must be UTF-8 encoded: {subtitle_path}") from exc
    return content, ("srt" if suffix == ".srt" else "vtt")


def render_stored_track_to_srt(*, job: JobDetail, track: StoredSubtitleTrack, track_index: int) -> str:
    if track.source_kind == "edited-transcript":
        return render_srt(job.transcript_segments)

    subtitle_path_raw = track.subtitle_path.strip() if track.subtitle_path else ""
    if not subtitle_path_raw:
        raise HTTPException(
            status_code=400,
            detail=f"Track {track.track_id} has no subtitle_path.",
        )

    subtitle_path = Path(subtitle_path_raw).expanduser()
    content, format_name = load_subtitle_text_from_path(subtitle_path)
    if format_name == "srt":
        return content

    parsed_segments = parse_subtitle_text_to_segments(
        content=content,
        format_name="vtt",
        job_id=f"{job.job_id}-track-{track_index + 1:02d}",
    )
    return render_srt(parsed_segments)


def resolve_export_tracks(job: JobDetail, track_ids: list[str]) -> list[StoredSubtitleTrack]:
    base_job = ensure_default_edited_track(job)
    available_tracks = [track for track in base_job.subtitle_tracks if track.is_active]
    if not available_tracks:
        raise HTTPException(status_code=400, detail="No active subtitle tracks available for export.")

    edited_track = next(
        (track for track in available_tracks if track.source_kind == "edited-transcript"),
        None,
    )

    if not track_ids:
        ordered = [track for track in available_tracks if track.source_kind == "edited-transcript"]
        ordered.extend(track for track in available_tracks if track.source_kind != "edited-transcript")
        return ordered

    selected: list[StoredSubtitleTrack] = []
    for track_id in track_ids:
        matched = next((track for track in available_tracks if track.track_id == track_id), None)
        if matched is None:
            raise HTTPException(status_code=400, detail=f"Subtitle track not found or inactive: {track_id}")
        selected.append(matched)

    if edited_track and not any(track.track_id == edited_track.track_id for track in selected):
        selected = [edited_track, *selected]
    return selected


def export_softsub_mp4(
    job: JobDetail,
    output_path: Path,
    *,
    track_ids: list[str],
) -> None:
    ffmpeg_binary = shutil.which("ffmpeg")
    if ffmpeg_binary is None:
        raise HTTPException(status_code=400, detail="ffmpeg is not installed.")
    if not job.media_metadata or job.media_metadata.has_video is not True:
        raise HTTPException(
            status_code=400,
            detail="Soft-subtitle MP4 export requires a video job.",
        )

    source_path = Path(job.media_path)
    if not source_path.exists() or not source_path.is_file():
        raise HTTPException(
            status_code=400,
            detail=f"Source media is missing: {source_path}",
        )

    normalized_tracks = resolve_export_tracks(job, track_ids)

    with tempfile.TemporaryDirectory(prefix="subtitle-softsub-") as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        temporary_track_paths: list[Path] = []
        for track_index, track in enumerate(normalized_tracks):
            srt_content = render_stored_track_to_srt(job=job, track=track, track_index=track_index)
            subtitles_path = temp_dir / f"{job.job_id}.track-{track_index + 1:02d}.srt"
            subtitles_path.write_text(srt_content, encoding="utf-8")
            temporary_track_paths.append(subtitles_path)

        command = [
            ffmpeg_binary,
            "-y",
            "-i",
            str(source_path),
        ]
        for subtitles_path in temporary_track_paths:
            command.extend(["-i", str(subtitles_path)])

        command.extend([
            "-map",
            "0:v?",
            "-map",
            "0:a?",
        ])
        for input_index in range(1, len(temporary_track_paths) + 1):
            command.extend(["-map", f"{input_index}:0"])

        command.extend([
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-c:s",
            "mov_text",
        ])

        for subtitle_index, track in enumerate(normalized_tracks):
            normalized_language = track.language.strip() or f"und-{subtitle_index + 1}"
            normalized_label = track.label.strip() or f"Subtitle Track {subtitle_index + 1}"
            command.extend(
                [
                    f"-metadata:s:s:{subtitle_index}",
                    f"language={normalized_language}",
                    f"-metadata:s:s:{subtitle_index}",
                    f"title={normalized_label}",
                ]
            )
            if track.is_default:
                command.extend([f"-disposition:s:{subtitle_index}", "default"])

        command.append(str(output_path))
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=600,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Failed to run ffmpeg export: {exc}",
            ) from exc

        if result.returncode != 0:
            stderr_message = (result.stderr or "").strip()
            detail = stderr_message if stderr_message else "Unknown ffmpeg error."
            raise HTTPException(
                status_code=400,
                detail=f"ffmpeg soft-subtitle export failed: {detail}",
            )


def find_transcription_cli() -> tuple[str | None, list[str] | None]:
    whisper_binary = shutil.which("whisper")
    if whisper_binary:
        return "whisper", [whisper_binary]
    return None, None


def try_local_cli_transcription(
    *,
    media_path: Path,
    job_id: str,
    media_metadata: MediaMetadata | None,
) -> tuple[list[TranscriptSegment] | None, str, str]:
    ffmpeg_binary = shutil.which("ffmpeg")
    if ffmpeg_binary is None:
        return None, "placeholder", "placeholder-fallback:ffmpeg-missing"

    cli_name, cli_command_prefix = find_transcription_cli()
    if cli_name is None or cli_command_prefix is None:
        return None, "placeholder", "placeholder-fallback:transcriber-missing"

    with tempfile.TemporaryDirectory(prefix="subtitle-workstation-") as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        audio_path = temp_dir / f"{job_id}.wav"
        try:
            extract_result = subprocess.run(
                [
                    ffmpeg_binary,
                    "-y",
                    "-i",
                    str(media_path),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    str(audio_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None, "placeholder", "placeholder-fallback:ffmpeg-extract-failed"
        if extract_result.returncode != 0 or not audio_path.exists():
            return None, "placeholder", "placeholder-fallback:ffmpeg-extract-failed"

        if cli_name == "whisper":
            cli_command = [
                *cli_command_prefix,
                str(audio_path),
                "--model",
                os.getenv("LOCAL_WHISPER_MODEL", "tiny"),
                "--task",
                "transcribe",
                "--output_format",
                "txt",
                "--output_dir",
                str(temp_dir),
            ]
            try:
                cli_result = subprocess.run(
                    cli_command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
            except (subprocess.TimeoutExpired, OSError):
                return None, "placeholder", "placeholder-fallback:transcriber-run-failed"
            if cli_result.returncode != 0:
                return None, "placeholder", "placeholder-fallback:transcriber-error"

            transcript_path = temp_dir / f"{audio_path.stem}.txt"
            if not transcript_path.exists():
                return None, "placeholder", "placeholder-fallback:transcriber-no-output"

            transcript_text = transcript_path.read_text(encoding="utf-8").strip()
            real_segments = split_text_into_segments(
                text=transcript_text,
                job_id=job_id,
                total_duration_seconds=(
                    media_metadata.duration_seconds if media_metadata else None
                ),
            )
            if not real_segments:
                return None, "placeholder", "placeholder-fallback:transcriber-empty"
            return real_segments, "real-cli", "real-cli:whisper"

    return None, "placeholder", "placeholder-fallback:unsupported-transcriber"


def create_ingest_job(*, media_path: Path) -> IngestResponse:
    # TODO: Add Whisper/WhisperX pipeline kickoff for transcription + alignment.
    # TODO: Add Pyannote diarization job kickoff.
    # TODO: Replace synthetic job_id with persistent queue-backed identifier.
    media_metadata = build_media_metadata(media_path)

    global next_job_id
    synthetic_job_id = f"job-placeholder-{next_job_id:03d}"
    next_job_id += 1
    stage: JobStage = "queued"
    transcription_mode: Literal["placeholder", "real-cli"] = "placeholder"
    transcription_source = "placeholder-synthetic"
    transcript_segments = build_placeholder_segments(
        stage=stage,
        job_id=synthetic_job_id,
        media_metadata=media_metadata,
    )
    local_segments, detected_mode, detected_source = try_local_cli_transcription(
        media_path=media_path,
        job_id=synthetic_job_id,
        media_metadata=media_metadata,
    )
    if local_segments:
        transcript_segments = local_segments
        transcription_mode = detected_mode
        transcription_source = detected_source
    else:
        transcription_mode = "placeholder"
        transcription_source = detected_source

    now = datetime.now(timezone.utc).isoformat()
    jobs.append(
        JobDetail(
            job_id=synthetic_job_id,
            kind="ingest",
            media_path=str(media_path),
            media_metadata=media_metadata,
            transcription_mode=transcription_mode,
            transcription_source=transcription_source,
            stage=stage,
            progress_percent=STAGE_PROGRESS[stage],
            created_at=now,
            updated_at=now,
            transcript_segments=transcript_segments,
        )
    )
    save_state()
    accepted_mode = "real local transcription" if transcription_mode == "real-cli" else "placeholder fallback"
    return IngestResponse(
        job_id=synthetic_job_id,
        status="queued",
        message=f"Ingest accepted ({accepted_mode}, source={transcription_source}) for: {media_path}",
    )


@app.post("/ingest", response_model=IngestResponse)
def ingest(payload: IngestRequest) -> IngestResponse:
    normalized_path = Path(payload.media_path.strip()).expanduser()
    if not payload.media_path.strip():
        raise HTTPException(status_code=400, detail="media_path is required.")
    if not normalized_path.exists():
        raise HTTPException(
            status_code=400,
            detail=f"Media path does not exist: {normalized_path}",
        )
    if not normalized_path.is_file():
        raise HTTPException(
            status_code=400,
            detail=f"Media path is not a file: {normalized_path}",
        )

    return create_ingest_job(media_path=normalized_path)


@app.post("/ingest/upload", response_model=IngestResponse)
async def ingest_upload(
    file: UploadFile = File(...),
    original_path: str | None = Form(default=None),
) -> IngestResponse:
    file_name = Path(file.filename or "upload.bin").name
    suffix = Path(file_name).suffix
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

    global next_job_id
    upload_stem = f"upload-{next_job_id:03d}"
    stored_path = UPLOADS_DIR / f"{upload_stem}{suffix}"

    try:
        with stored_path.open("wb") as output_file:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                output_file.write(chunk)
    finally:
        await file.close()

    if stored_path.stat().st_size == 0:
        stored_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    ingest_response = create_ingest_job(media_path=stored_path)
    if original_path:
        ingest_response.message += f" (uploaded from {original_path})"
    return ingest_response


@app.get("/jobs", response_model=list[JobSummary])
def list_jobs() -> list[JobSummary]:
    # TODO: Back this with a real job store (database or durable local state).
    # TODO: Return stage-level progress for ffmpeg decode, WhisperX align, and diarization.
    return [summarize_job(job) for job in jobs]


@app.get("/jobs/{job_id}", response_model=JobDetail)
def get_job(job_id: str) -> JobDetail:
    for job in jobs:
        if job.job_id == job_id:
            return renumber_tracks(ensure_default_edited_track(job))
    raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")


@app.put("/jobs/{job_id}/transcript", response_model=JobDetail)
def update_job_transcript(job_id: str, payload: UpdateTranscriptRequest) -> JobDetail:
    for index, job in enumerate(jobs):
        if job.job_id != job_id:
            continue

        seen_segment_ids: set[str] = set()
        for segment in payload.transcript_segments:
            if not segment.segment_id:
                raise HTTPException(status_code=400, detail="segment_id is required.")
            if segment.segment_id in seen_segment_ids:
                raise HTTPException(
                    status_code=400,
                    detail=f"Duplicate segment_id detected: {segment.segment_id}",
                )
            seen_segment_ids.add(segment.segment_id)

        updated_job = ensure_default_edited_track(job).model_copy(
            update={
                "transcript_segments": payload.transcript_segments,
                "transcript_is_edited": True,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        jobs[index] = updated_job
        save_state()
        return updated_job

    raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")


@app.post("/jobs/{job_id}/import-subtitles", response_model=JobDetail)
async def import_job_subtitles(job_id: str, file: UploadFile = File(...)) -> JobDetail:
    file_name = (file.filename or "").lower()
    if file_name.endswith(".srt"):
        format_name: Literal["srt", "vtt"] = "srt"
    elif file_name.endswith(".vtt"):
        format_name = "vtt"
    else:
        await file.close()
        raise HTTPException(status_code=400, detail="Subtitle import supports only .srt or .vtt files.")

    try:
        content_bytes = await file.read()
    finally:
        await file.close()

    if not content_bytes:
        raise HTTPException(status_code=400, detail="Subtitle file is empty.")

    try:
        content = content_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Subtitle file must be UTF-8 encoded.") from exc

    parsed_segments = parse_subtitle_text_to_segments(
        content=content,
        format_name=format_name,
        job_id=job_id,
    )

    for index, job in enumerate(jobs):
        if job.job_id != job_id:
            continue
        base_job = ensure_default_edited_track(job)
        track_path = DATA_DIR / "tracks" / f"{job_id}-uploaded-{len(base_job.subtitle_tracks)+1:03d}.{format_name}"
        track_path.parent.mkdir(parents=True, exist_ok=True)
        track_path.write_text(content, encoding="utf-8")
        new_track = create_stored_track(
            job=base_job,
            source_kind="uploaded-subtitle",
            format_name=format_name,
            subtitle_path=str(track_path),
            label=Path(file.filename or f"Uploaded {format_name.upper()}").stem,
            language="eng",
            origin_note="Uploaded subtitle file",
            is_default=False,
        )
        updated_job = renumber_tracks(base_job.model_copy(
            update={
                "transcript_segments": parsed_segments,
                "transcript_is_edited": True,
                "transcription_mode": "real-cli",
                "transcription_source": f"imported-{format_name}",
                "subtitle_tracks": [*base_job.subtitle_tracks, new_track],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ))
        jobs[index] = updated_job
        save_state()
        return updated_job

    raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")


@app.get("/jobs/{job_id}/subtitle-streams")
def get_job_subtitle_streams(job_id: str) -> list[dict]:
    job = get_job(job_id)
    source_path = Path(job.media_path)
    if not source_path.exists() or not source_path.is_file():
        raise HTTPException(status_code=400, detail=f"Source media is missing: {source_path}")
    return list_subtitle_streams(source_path)


@app.get("/jobs/{job_id}/subtitle-tracks", response_model=list[StoredSubtitleTrack])
def get_job_subtitle_tracks(job_id: str) -> list[StoredSubtitleTrack]:
    job = get_job(job_id)
    return job.subtitle_tracks


@app.post("/jobs/{job_id}/subtitle-tracks", response_model=JobDetail)
def create_job_subtitle_track(job_id: str, payload: CreateSubtitleTrackRequest) -> JobDetail:
    for index, job in enumerate(jobs):
        if job.job_id != job_id:
            continue
        base_job = ensure_default_edited_track(job)
        subtitle_path = Path(payload.subtitle_path.strip()).expanduser()
        if not payload.subtitle_path.strip():
            raise HTTPException(status_code=400, detail="subtitle_path is required.")
        content, format_name = load_subtitle_text_from_path(subtitle_path)
        parsed_segments = parse_subtitle_text_to_segments(
            content=content,
            format_name=format_name,
            job_id=job_id,
        )
        stored_track_path = DATA_DIR / "tracks" / f"{job_id}-manual-{len(base_job.subtitle_tracks)+1:03d}.{format_name}"
        stored_track_path.parent.mkdir(parents=True, exist_ok=True)
        stored_track_path.write_text(content if format_name == "srt" else render_srt(parsed_segments), encoding="utf-8")
        new_track = create_stored_track(
            job=base_job,
            source_kind="uploaded-subtitle",
            format_name="srt",
            subtitle_path=str(stored_track_path),
            label=payload.label,
            language=payload.language,
            origin_note=f"Manual subtitle track import from {subtitle_path}",
            is_default=payload.is_default,
        )
        next_tracks = [*base_job.subtitle_tracks, new_track]
        updated_job = renumber_tracks(base_job.model_copy(
            update={
                "subtitle_tracks": next_tracks,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ))
        if payload.is_default:
            updated_job = assign_default_track(updated_job, updated_job.subtitle_tracks[-1].track_id)
        jobs[index] = updated_job
        save_state()
        return updated_job

    raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")


@app.delete("/jobs/{job_id}/subtitle-tracks/{track_id}", response_model=JobDetail)
def delete_job_subtitle_track(job_id: str, track_id: str) -> JobDetail:
    for index, job in enumerate(jobs):
        if job.job_id != job_id:
            continue
        base_job = ensure_default_edited_track(job)
        next_tracks = [track for track in base_job.subtitle_tracks if track.track_id != track_id or track.source_kind == "edited-transcript"]
        updated_job = renumber_tracks(base_job.model_copy(
            update={
                "subtitle_tracks": next_tracks,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ))
        if not any(track.is_default for track in updated_job.subtitle_tracks):
            updated_job = assign_default_track(updated_job, updated_job.subtitle_tracks[0].track_id if updated_job.subtitle_tracks else None)
        jobs[index] = updated_job
        save_state()
        return updated_job

    raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")


@app.get("/jobs/{job_id}/sidecar-subtitles")
def get_job_sidecar_subtitles(job_id: str) -> list[dict]:
    job = get_job(job_id)
    source_path = Path(job.media_path)
    if not source_path.exists() or not source_path.is_file():
        raise HTTPException(status_code=400, detail=f"Source media is missing: {source_path}")
    return find_sidecar_subtitle_candidates(source_path)


@app.post("/jobs/{job_id}/import-sidecar-subtitles", response_model=JobDetail)
def import_sidecar_subtitles(job_id: str) -> JobDetail:
    for index, job in enumerate(jobs):
        if job.job_id != job_id:
            continue
        source_path = Path(job.media_path)
        candidates = find_sidecar_subtitle_candidates(source_path)
        if not candidates:
            raise HTTPException(status_code=400, detail="No sidecar subtitle files found next to the video.")
        chosen_path = Path(str(candidates[0]["path"]))
        parsed_segments, source_label = import_sidecar_subtitle_path(
            job=job,
            subtitle_path=chosen_path,
        )
        base_job = ensure_default_edited_track(job)
        new_track = create_stored_track(
            job=base_job,
            source_kind="sidecar-subtitle",
            format_name="srt" if chosen_path.suffix.lower() == ".srt" else "vtt",
            subtitle_path=str(chosen_path),
            label=chosen_path.stem,
            language="eng",
            origin_note=source_label,
            is_default=False,
        )
        updated_job = renumber_tracks(base_job.model_copy(
            update={
                "transcript_segments": parsed_segments,
                "transcript_is_edited": True,
                "transcription_mode": "real-cli",
                "transcription_source": source_label,
                "subtitle_tracks": [*base_job.subtitle_tracks, new_track],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ))
        jobs[index] = updated_job
        save_state()
        return updated_job

    raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")


@app.post("/jobs/{job_id}/import-embedded-subtitles", response_model=JobDetail)
def import_embedded_subtitles(
    job_id: str, payload: ImportEmbeddedSubtitleTrackRequest
) -> JobDetail:
    for index, job in enumerate(jobs):
        if job.job_id != job_id:
            continue
        parsed_segments, source_label = extract_embedded_subtitle_segments(
            job=job,
            stream_index=payload.stream_index,
        )
        base_job = ensure_default_edited_track(job)
        embedded_track_path = DATA_DIR / "tracks" / f"{job_id}-embedded-{len(base_job.subtitle_tracks)+1:03d}.srt"
        embedded_track_path.parent.mkdir(parents=True, exist_ok=True)
        embedded_track_path.write_text(render_srt(parsed_segments), encoding="utf-8")
        new_track = create_stored_track(
            job=base_job,
            source_kind="embedded-subtitle",
            format_name="srt",
            subtitle_path=str(embedded_track_path),
            label=source_label,
            language="eng",
            origin_note=source_label,
            is_default=False,
        )
        updated_job = renumber_tracks(base_job.model_copy(
            update={
                "transcript_segments": parsed_segments,
                "transcript_is_edited": True,
                "transcription_mode": "real-cli",
                "transcription_source": source_label,
                "subtitle_tracks": [*base_job.subtitle_tracks, new_track],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ))
        jobs[index] = updated_job
        save_state()
        return updated_job

    raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")


@app.get("/jobs/{job_id}/export/srt")
def export_job_srt(job_id: str) -> Response:
    job = get_job(job_id)
    content = render_srt(job.transcript_segments)
    return Response(
        content=content,
        media_type="application/x-subrip",
        headers={"Content-Disposition": f'attachment; filename="{job_id}.srt"'},
    )


@app.get("/jobs/{job_id}/export/vtt")
def export_job_vtt(job_id: str) -> Response:
    job = get_job(job_id)
    content = render_vtt(job.transcript_segments)
    return Response(
        content=content,
        media_type="text/vtt; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{job_id}.vtt"'},
    )


@app.post("/jobs/{job_id}/export/mp4-softsub")
def export_job_softsub_mp4(
    job_id: str, payload: ExportSoftSubtitleMp4Request
) -> dict[str, str]:
    job = get_job(job_id)
    output_path = ensure_softsub_export_target(payload.output_path)
    export_softsub_mp4(
        job,
        output_path,
        track_ids=payload.track_ids,
    )
    return {
        "status": "ok",
        "output_path": str(output_path),
        "message": f"Soft-subtitled MP4 exported to {output_path}",
    }


@app.post("/jobs/{job_id}/advance", response_model=JobSummary)
def advance_job(job_id: str) -> JobSummary:
    # TODO: Replace this placeholder stage progression with worker-reported pipeline state.
    for index, job in enumerate(jobs):
        if job.job_id != job_id:
            continue
        current_stage_index = STAGE_ORDER.index(job.stage)
        next_stage = STAGE_ORDER[min(current_stage_index + 1, len(STAGE_ORDER) - 1)]
        updated_job = job.model_copy(
            update={
                "stage": next_stage,
                "progress_percent": STAGE_PROGRESS[next_stage],
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "transcript_segments": (
                    build_placeholder_segments(
                        stage=next_stage,
                        job_id=job.job_id,
                        media_metadata=job.media_metadata,
                    )
                    if job.transcription_mode == "placeholder"
                    and not job.transcript_is_edited
                    else job.transcript_segments
                ),
            }
        )
        jobs[index] = updated_job
        save_state()
        return summarize_job(updated_job)

    raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
