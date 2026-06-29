from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from html import escape
import importlib.util
import json
import logging
import mimetypes
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from xml.etree import ElementTree as ET
from typing import Any, Callable, Literal
import zipfile

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field, model_validator

app = FastAPI(title="Subtitle Workstation API", version="0.1.0")
logger = logging.getLogger("subtitle_workstation")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://((localhost|127\.0\.0\.1|10\.0\.0\.[0-9]+|192\.168\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[0-1])\.[0-9]+\.[0-9]+)(:\d+)?|kuon\.ai)",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class IngestRequest(BaseModel):
    media_path: str = Field(..., description="Local media file path")
    legacy_subtitle_path: str | None = Field(
        default=None,
        description="Optional local .srt or .vtt path to auto-apply after current timing is generated.",
    )


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


JobStage = Literal["queued", "probing", "transcribing", "aligned", "diarized", "ready"]

STAGE_ORDER: tuple[JobStage, ...] = (
    "queued",
    "probing",
    "transcribing",
    "aligned",
    "diarized",
    "ready",
)

STAGE_PROGRESS: dict[JobStage, int] = {
    "queued": 5,
    "probing": 20,
    "transcribing": 55,
    "aligned": 75,
    "diarized": 90,
    "ready": 100,
}

STAGE_LABELS: dict[JobStage, str] = {
    "queued": "Queued",
    "probing": "Probing media",
    "transcribing": "Transcribing",
    "aligned": "Aligned",
    "diarized": "Speaker tagging",
    "ready": "Ready",
}

STAGE_DESCRIPTIONS: dict[JobStage, str] = {
    "queued": "Job accepted. Waiting to start local processing.",
    "probing": "Inspecting the media file and streams.",
    "transcribing": "Generating transcript text from audio.",
    "aligned": "Refining subtitle timing and building editable segments.",
    "diarized": "Applying speaker labels when available.",
    "ready": "Processing complete. Review and export are ready.",
}


class JobSummary(BaseModel):
    job_id: str
    kind: str
    media_path: str
    media_metadata: MediaMetadata | None = None
    transcription_mode: Literal["placeholder", "real-cli"] = "placeholder"
    transcription_source: str = "placeholder-synthetic"
    timing_source: str = "placeholder-synthetic"
    stage: JobStage
    progress_percent: int = Field(..., ge=0, le=100)
    stage_label: str = "Queued"
    stage_description: str = "Job accepted. Waiting to start local processing."
    artifact_count: int = Field(default=0, ge=0)
    created_at: str
    updated_at: str


RetimeStatus = Literal["matched", "low-confidence", "new-only", "corrected", "sore-thumb"]


class CorrectionSuggestion(BaseModel):
    wrong_text: str
    corrected_text: str
    confidence: float = Field(..., ge=0, le=1)
    kind: Literal["phrase", "casing", "llm-candidate"] = "phrase"
    status: Literal["applied", "suggested"] = "suggested"
    source_segment_id: str | None = None
    note: str | None = None


class TranscriptSegment(BaseModel):
    segment_id: str
    start_seconds: float = Field(..., ge=0)
    end_seconds: float = Field(..., gt=0)
    text: str
    speaker: str | None = None
    retime_confidence: float | None = Field(default=None, ge=0, le=1)
    retime_status: RetimeStatus | None = None
    retime_note: str | None = None
    correction_suggestions: list[CorrectionSuggestion] = Field(default_factory=list)


class StoredSubtitleTrack(BaseModel):
    track_id: str
    source_kind: Literal[
        "edited-transcript",
        "uploaded-subtitle",
        "sidecar-subtitle",
        "embedded-subtitle",
        "retimed-edits",
        "pre-retime-snapshot",
    ]
    format_name: Literal["srt", "vtt"]
    language: str = "eng"
    label: str = "English Subtitles"
    subtitle_path: str | None = None
    is_default: bool = False
    is_active: bool = True
    origin_note: str | None = None
    created_at: str
    updated_at: str


class GeneratedArtifact(BaseModel):
    artifact_id: str
    artifact_kind: Literal["transcript-srt", "transcript-vtt", "video-mp4-softsub", "package-scorm12", "package-scorm2004", "package-aicc", "package-xapi", "package-cmi5"]
    format_name: Literal["srt", "vtt", "mp4-softsub", "scorm12", "scorm2004", "aicc", "xapi", "cmi5"]
    file_name: str
    artifact_path: str
    download_url: str | None = None
    size_bytes: int = Field(..., ge=0)
    transcript_segment_count: int = Field(..., ge=0)
    created_at: str

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_shape(cls, raw: object) -> object:
        if not isinstance(raw, dict):
            return raw
        payload = dict(raw)
        legacy_kind = payload.get("kind")
        if "artifact_kind" not in payload and isinstance(legacy_kind, str):
            if legacy_kind.endswith("vtt"):
                payload["artifact_kind"] = "transcript-vtt"
            elif legacy_kind.endswith("mp4-softsub") or legacy_kind.endswith("mp4"):
                payload["artifact_kind"] = "video-mp4-softsub"
            else:
                payload["artifact_kind"] = "transcript-srt"
        if "format_name" not in payload:
            kind_value = str(payload.get("artifact_kind", ""))
            if kind_value.endswith("vtt"):
                payload["format_name"] = "vtt"
            elif kind_value.endswith("mp4-softsub") or kind_value.endswith("mp4"):
                payload["format_name"] = "mp4-softsub"
            elif kind_value.startswith("package-"):
                payload["format_name"] = kind_value.removeprefix("package-")
            else:
                payload["format_name"] = "srt"
        if "file_name" not in payload and isinstance(payload.get("filename"), str):
            payload["file_name"] = payload["filename"]
        if "artifact_path" not in payload and isinstance(payload.get("relative_path"), str):
            payload["artifact_path"] = payload["relative_path"]
        if "download_url" not in payload:
            payload["download_url"] = None
        if "size_bytes" not in payload:
            payload["size_bytes"] = 0
        if "transcript_segment_count" not in payload:
            payload["transcript_segment_count"] = 0
        return payload


class JobDetail(JobSummary):
    transcript_segments: list[TranscriptSegment] = Field(default_factory=list)
    transcript_is_edited: bool = False
    subtitle_tracks: list[StoredSubtitleTrack] = Field(default_factory=list)
    artifacts: list[GeneratedArtifact] = Field(default_factory=list)
    pending_legacy_subtitle_path: str | None = None
    pending_legacy_subtitle_name: str | None = None


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
    output_path: str | None = Field(default=None, description="Destination MP4 path")
    output_dir: str | None = Field(default=None, description="Optional output directory for compatibility helpers")
    output_filename: str | None = Field(default=None, description="Optional MP4 filename used with output_dir")
    download: bool = Field(default=False, description="When true, return the MP4 as a browser download response")
    track_ids: list[str] = Field(
        default_factory=list,
        description="Stored subtitle track ids to mux into the MP4. Empty means export active tracks.",
    )

    @model_validator(mode="after")
    def validate_output_target(self) -> "ExportSoftSubtitleMp4Request":
        has_output_path = bool(self.output_path and self.output_path.strip())
        has_output_filename = bool(self.output_filename and self.output_filename.strip())
        has_split_target = bool(
            self.output_dir and self.output_dir.strip() and has_output_filename
        )
        if has_output_path or has_split_target or has_output_filename or self.download:
            return self
        raise ValueError("output_path is required, or provide output_filename, or provide both output_dir and output_filename")

    def resolved_output_path(self) -> str:
        if self.output_path and self.output_path.strip():
            return self.output_path
        if self.output_dir and self.output_dir.strip() and self.output_filename and self.output_filename.strip():
            return str(Path(self.output_dir.strip()).expanduser() / self.output_filename.strip())
        fallback_name = self.output_filename.strip() if self.output_filename and self.output_filename.strip() else "export.softsubs.mp4"
        return str(Path(tempfile.gettempdir()) / fallback_name)


class BuildArtifactRequest(BaseModel):
    output_filename: str | None = Field(
        default=None,
        description="Optional human-readable output filename or base name.",
    )


class CreateSubtitleTrackRequest(BaseModel):
    language: str = "eng"
    label: str = "English Subtitles"
    subtitle_path: str = Field(..., description="Path to a local .srt or .vtt file")
    is_default: bool = False


class ImportEmbeddedSubtitleTrackRequest(BaseModel):
    stream_index: int | None = Field(default=None, description="Subtitle stream index")


class RetimedSegmentReport(BaseModel):
    segment_id: str
    old_segment_id: str | None = None
    confidence: float = Field(..., ge=0, le=1)
    status: RetimeStatus
    note: str | None = None
    correction_suggestions: list[CorrectionSuggestion] = Field(default_factory=list)


class RetimeEditedSubtitlesReport(BaseModel):
    source_file_name: str
    source_format: Literal["srt", "vtt"]
    matched_segments: int = Field(..., ge=0)
    low_confidence_segments: int = Field(..., ge=0)
    unmatched_old_segments: int = Field(..., ge=0)
    unmatched_new_segments: int = Field(..., ge=0)
    average_confidence: float = Field(..., ge=0, le=1)
    threshold: float = Field(..., ge=0, le=1)
    created_at: str
    learned_corrections: list[CorrectionSuggestion] = Field(default_factory=list)
    applied_corrections: int = Field(default=0, ge=0)
    sore_thumb_segments: int = Field(default=0, ge=0)
    segments: list[RetimedSegmentReport] = Field(default_factory=list)


class RetimeEditedSubtitlesResponse(JobDetail):
    retime_report: RetimeEditedSubtitlesReport


LlmCorrectionProvider = Callable[[str, str, float, str | None], list[CorrectionSuggestion]]


ScormVersion = Literal["scorm12", "scorm2004"]


class ScormValidationIssue(BaseModel):
    level: Literal["error", "warning"]
    message: str


class ScormPackageSummary(BaseModel):
    package_id: str
    file_name: str
    scorm_version: ScormVersion | None = None
    title: str
    launch_path: str | None = None
    valid: bool = False
    uploaded_at: str
    updated_at: str
    issue_count: int = Field(default=0, ge=0)


class ScormPackageDetail(ScormPackageSummary):
    extracted_dir: str
    issues: list[ScormValidationIssue] = Field(default_factory=list)
    viewer_url: str | None = None
    attempts: list[str] = Field(default_factory=list)


class ScormAttemptSummary(BaseModel):
    attempt_id: str
    package_id: str
    learner_id: str = "local-learner"
    learner_name: str = "Local Learner"
    registration_id: str
    launched_at: str
    updated_at: str
    completed: bool = False
    score_raw: float | None = None
    location: str | None = None
    suspend_data: str | None = None


class ScormRuntimePayload(BaseModel):
    data: dict[str, str] = Field(default_factory=dict)
    completed: bool = False


class ScormRuntimeResponse(BaseModel):
    attempt: ScormAttemptSummary
    runtime_data: dict[str, str] = Field(default_factory=dict)


jobs: list[JobDetail] = []
next_job_id = 1
jobs_lock = threading.Lock()
ingest_worker_lock = threading.Lock()
active_ingest_job_id: str | None = None
scorm_lock = threading.RLock()
DATA_DIR = Path(__file__).parent / "data"
UPLOADS_DIR = DATA_DIR / "uploads"
TRACKS_DIR = DATA_DIR / "tracks"
ARTIFACTS_DIR = DATA_DIR / "artifacts"
SCORM_DIR = DATA_DIR / "scorm"
SCORM_UPLOADS_DIR = SCORM_DIR / "uploads"
SCORM_EXTRACTED_DIR = SCORM_DIR / "packages"
STORE_PATH = DATA_DIR / "jobs.json"
SCORM_STATE_PATH = SCORM_DIR / "state.json"
PACKAGE_FORMATS: tuple[str, ...] = ("scorm12", "scorm2004", "aicc", "xapi", "cmi5")
DATA_RETENTION_MAX_AGE = timedelta(hours=24)
DATA_RETENTION_SWEEP_INTERVAL = timedelta(minutes=15)
UPLOAD_MAX_BYTES = int(os.getenv("SRT_UPLOAD_MAX_BYTES", str(2 * 1024 * 1024 * 1024)))
last_retention_cleanup_at: datetime | None = None
scorm_packages: list[ScormPackageDetail] = []
scorm_attempts: dict[str, ScormAttemptSummary] = {}
scorm_runtime_store: dict[str, dict[str, str]] = {}


def _write_state_unlocked() -> None:
    payload = {
        "next_job_id": next_job_id,
        "jobs": [job.model_dump() for job in jobs],
    }
    STORE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def save_state() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with jobs_lock:
        _write_state_unlocked()
    prune_expired_data()


def _write_scorm_state_unlocked() -> None:
    SCORM_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "packages": [package.model_dump() for package in scorm_packages],
        "attempts": {attempt_id: attempt.model_dump() for attempt_id, attempt in scorm_attempts.items()},
        "runtime": scorm_runtime_store,
    }
    SCORM_STATE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def save_scorm_state() -> None:
    SCORM_DIR.mkdir(parents=True, exist_ok=True)
    with scorm_lock:
        _write_scorm_state_unlocked()


def parse_state_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def path_within_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def job_last_touched(job: JobDetail) -> datetime:
    return (
        parse_state_datetime(job.updated_at)
        or parse_state_datetime(job.created_at)
        or datetime.now(timezone.utc)
    )


def scorm_package_last_touched(package: ScormPackageDetail) -> datetime:
    return (
        parse_state_datetime(package.updated_at)
        or parse_state_datetime(package.uploaded_at)
        or datetime.now(timezone.utc)
    )


def collect_scorm_owned_paths(package: ScormPackageDetail) -> set[Path]:
    owned_paths: set[Path] = set()

    upload_path = SCORM_UPLOADS_DIR / package.file_name
    if path_within_root(upload_path, SCORM_UPLOADS_DIR):
        owned_paths.add(upload_path)

    extracted_path = Path(package.extracted_dir).expanduser()
    if path_within_root(extracted_path, SCORM_EXTRACTED_DIR):
        owned_paths.add(extracted_path)

    return owned_paths


def collect_job_owned_paths(job: JobDetail) -> set[Path]:
    owned_paths: set[Path] = set()

    media_path = Path(job.media_path).expanduser()
    if path_within_root(media_path, UPLOADS_DIR):
        owned_paths.add(media_path)

    for track in job.subtitle_tracks:
        if not track.subtitle_path:
            continue
        subtitle_path = Path(track.subtitle_path).expanduser()
        if path_within_root(subtitle_path, TRACKS_DIR):
            owned_paths.add(subtitle_path)

    for artifact in job.artifacts:
        artifact_path = Path(artifact.artifact_path).expanduser()
        if not artifact_path.is_absolute():
            artifact_path = ARTIFACTS_DIR / artifact_path
        if path_within_root(artifact_path, ARTIFACTS_DIR):
            owned_paths.add(artifact_path)

    artifact_dir = ARTIFACTS_DIR / job.job_id
    if path_within_root(artifact_dir, ARTIFACTS_DIR):
        owned_paths.add(artifact_dir)

    return owned_paths


def remove_path_if_present(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except FileNotFoundError:
        return


def prune_expired_data(*, force: bool = False) -> None:
    global jobs, last_retention_cleanup_at

    now = datetime.now(timezone.utc)
    if (
        not force
        and last_retention_cleanup_at is not None
        and now - last_retention_cleanup_at < DATA_RETENTION_SWEEP_INTERVAL
    ):
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = now - DATA_RETENTION_MAX_AGE
    paths_to_remove: set[Path] = set()
    state_changed = False

    with jobs_lock:
        retained_jobs: list[JobDetail] = []
        for job in jobs:
            if job_last_touched(job) < cutoff:
                paths_to_remove.update(collect_job_owned_paths(job))
                state_changed = True
            else:
                retained_jobs.append(job)
        if state_changed:
            jobs = retained_jobs
            _write_state_unlocked()

    for path in sorted(paths_to_remove, key=lambda candidate: (candidate.is_file(), len(candidate.parts)), reverse=True):
        remove_path_if_present(path)

    for root in (UPLOADS_DIR, TRACKS_DIR, ARTIFACTS_DIR):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if modified_at < cutoff:
                remove_path_if_present(path)
        for directory in sorted((path for path in root.rglob("*") if path.is_dir()), key=lambda item: len(item.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                continue

    last_retention_cleanup_at = now


def prune_expired_scorm_data(*, force: bool = False) -> None:
    global scorm_packages, scorm_attempts, scorm_runtime_store

    now = datetime.now(timezone.utc)
    if (
        not force
        and last_retention_cleanup_at is not None
        and now - last_retention_cleanup_at < DATA_RETENTION_SWEEP_INTERVAL
    ):
        return

    SCORM_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = now - DATA_RETENTION_MAX_AGE
    package_ids_to_remove: set[str] = set()
    attempt_ids_to_remove: set[str] = set()
    paths_to_remove: set[Path] = set()

    with scorm_lock:
        retained_packages: list[ScormPackageDetail] = []
        retained_attempts: dict[str, ScormAttemptSummary] = {}
        for package in scorm_packages:
            if scorm_package_last_touched(package) < cutoff:
                package_ids_to_remove.add(package.package_id)
                attempt_ids_to_remove.update(package.attempts)
                paths_to_remove.update(collect_scorm_owned_paths(package))
            else:
                retained_packages.append(package)

        for attempt_id, attempt in scorm_attempts.items():
            if attempt.package_id in package_ids_to_remove or attempt_id in attempt_ids_to_remove:
                attempt_ids_to_remove.add(attempt_id)
                continue
            retained_attempts[attempt_id] = attempt

        if package_ids_to_remove:
            scorm_packages = retained_packages
            scorm_attempts = retained_attempts
            scorm_runtime_store = {
                attempt_id: runtime
                for attempt_id, runtime in scorm_runtime_store.items()
                if attempt_id not in attempt_ids_to_remove
            }
            _write_scorm_state_unlocked()

    for path in sorted(paths_to_remove, key=lambda candidate: (candidate.is_file(), len(candidate.parts)), reverse=True):
        remove_path_if_present(path)

    for root in (SCORM_UPLOADS_DIR, SCORM_EXTRACTED_DIR):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if modified_at < cutoff:
                remove_path_if_present(path)
        for directory in sorted((path for path in root.rglob("*") if path.is_dir()), key=lambda item: len(item.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                continue


def summarize_job(job: JobDetail) -> JobSummary:
    return JobSummary.model_validate(
        job.model_dump() | {"artifact_count": len(job.artifacts)}
    )


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


def make_artifact_id(job_id: str, index: int) -> str:
    return f"{job_id}-artifact-{index:03d}"


def build_artifact_download_url(job_id: str, artifact_id: str) -> str:
    return f"/jobs/{job_id}/artifacts/{artifact_id}/download"


def to_artifact_relative_path(path: Path) -> str:
    return str(path.relative_to(ARTIFACTS_DIR))


def ensure_job_artifact_dir(job_id: str) -> Path:
    artifact_dir = ARTIFACTS_DIR / job_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return artifact_dir


def find_job_or_404(job_id: str) -> JobDetail:
    for job in jobs:
        if job.job_id == job_id:
            return job
    raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")


def get_job_artifact(job: JobDetail, artifact_id: str) -> GeneratedArtifact:
    for artifact in job.artifacts:
        if artifact.artifact_id == artifact_id:
            return artifact
    raise HTTPException(
        status_code=404,
        detail=f"Artifact not found for {job.job_id}: {artifact_id}",
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


def load_scorm_state() -> None:
    global scorm_packages, scorm_attempts, scorm_runtime_store
    SCORM_DIR.mkdir(parents=True, exist_ok=True)
    if not SCORM_STATE_PATH.exists():
        scorm_packages = []
        scorm_attempts = {}
        scorm_runtime_store = {}
        save_scorm_state()
        return

    try:
        raw_state = json.loads(SCORM_STATE_PATH.read_text(encoding="utf-8"))
        scorm_packages = [ScormPackageDetail.model_validate(item) for item in raw_state.get("packages", [])]
        scorm_attempts = {
            attempt_id: ScormAttemptSummary.model_validate(item)
            for attempt_id, item in raw_state.get("attempts", {}).items()
        }
        runtime_raw = raw_state.get("runtime", {})
        scorm_runtime_store = {
            str(attempt_id): {str(key): str(value) for key, value in values.items()}
            for attempt_id, values in runtime_raw.items()
            if isinstance(values, dict)
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        scorm_packages = []
        scorm_attempts = {}
        scorm_runtime_store = {}
        save_scorm_state()


@app.on_event("startup")
def on_startup() -> None:
    load_state()
    load_scorm_state()
    prune_expired_data(force=True)
    prune_expired_scorm_data(force=True)


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "subtitle-workstation-api",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def tool_version(command: str, *args: str, timeout: int = 8) -> dict[str, Any]:
    binary = shutil.which(command)
    if binary is None:
        return {"available": False, "path": None, "version": None}
    try:
        result = subprocess.run(
            [binary, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "available": True,
            "path": binary,
            "version": None,
            "error": type(exc).__name__,
        }
    version_text = (result.stdout or result.stderr or "").strip().splitlines()
    return {
        "available": True,
        "path": binary,
        "version": version_text[0] if version_text else None,
        "exit_code": result.returncode,
    }


@app.get("/diagnostics")
def diagnostics() -> dict[str, Any]:
    whisperx_command, whisperx_status = find_whisperx_command()
    whisper_command, _ = find_transcription_cli()
    with jobs_lock:
        job_count = len(jobs)
        ready_jobs = sum(1 for job in jobs if job.stage == "ready")
        queued_jobs = sum(1 for job in jobs if job.stage == "queued")
        transcribing_jobs = sum(1 for job in jobs if job.stage == "transcribing")

    return {
        "status": "ok",
        "service": "subtitle-workstation-api",
        "version": app.version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "runtime": {
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "cwd": str(Path.cwd()),
        },
        "paths": {
            "data_dir": str(DATA_DIR),
            "uploads_dir": str(UPLOADS_DIR),
            "tracks_dir": str(TRACKS_DIR),
            "artifacts_dir": str(ARTIFACTS_DIR),
        },
        "tools": {
            "ffmpeg": tool_version("ffmpeg", "-version"),
            "ffprobe": tool_version("ffprobe", "-version"),
            "whisper": {
                "available": whisper_command is not None,
                "path": shutil.which("whisper"),
            },
            "whisperx": {
                "available": whisperx_command is not None,
                "status": whisperx_status,
                "command": whisperx_command,
                "model": os.getenv("LOCAL_WHISPERX_MODEL", os.getenv("LOCAL_WHISPER_MODEL", "tiny")),
                "device": os.getenv("LOCAL_WHISPERX_DEVICE", "cpu"),
                "compute_type": os.getenv("LOCAL_WHISPERX_COMPUTE_TYPE", "int8"),
            },
        },
        "llm": {
            "enabled": env_flag("SRT_LLM_RECONCILIATION", False),
            "provider": "bedrock-claude-haiku",
            "model_id": os.getenv("CLAUDE_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
            "boto3_available": importlib.util.find_spec("boto3") is not None,
        },
        "ingest": {
            "active_job_id": active_ingest_job_id,
            "worker_busy": ingest_worker_lock.locked(),
            "upload_max_bytes": UPLOAD_MAX_BYTES,
            "job_count": job_count,
            "ready_jobs": ready_jobs,
            "queued_jobs": queued_jobs,
            "transcribing_jobs": transcribing_jobs,
        },
        "retention": {
            "max_age_seconds": int(DATA_RETENTION_MAX_AGE.total_seconds()),
            "sweep_interval_seconds": int(DATA_RETENTION_SWEEP_INTERVAL.total_seconds()),
            "last_cleanup_at": last_retention_cleanup_at.isoformat() if last_retention_cleanup_at else None,
        },
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


def make_scorm_package_id() -> str:
    return f"scorm-{int(time.time() * 1000)}"


def make_scorm_attempt_id() -> str:
    return f"attempt-{int(time.time() * 1000)}"


def sanitize_scorm_member_name(name: str) -> PurePosixPath:
    normalized = PurePosixPath(name.replace('\\', '/'))
    cleaned_parts = [part for part in normalized.parts if part not in ('', '.', '..')]
    return PurePosixPath(*cleaned_parts)


def detect_scorm_version(manifest_root: ET.Element) -> ScormVersion | None:
    schema = ''
    schemaversion = ''
    for element in manifest_root.iter():
        tag_name = element.tag.split('}')[-1].lower()
        text = (element.text or '').strip().lower()
        if tag_name == 'schema':
            schema = text
        if tag_name == 'schemaversion':
            schemaversion = text
    combined = f"{schema} {schemaversion}".strip()
    if '2004' in combined:
        return 'scorm2004'
    if '1.2' in combined or '1_2' in combined or 'cam 1.3' in combined:
        return 'scorm12'
    namespace_blob = ' '.join(str(value) for value in manifest_root.attrib.values()).lower()
    if 'adlcp_v1p3' in namespace_blob or 'imsss' in namespace_blob:
        return 'scorm2004'
    return 'scorm12' if 'adlcp' in namespace_blob else None


def validate_and_extract_scorm_package(upload_path: Path, package_id: str) -> ScormPackageDetail:
    issues: list[ScormValidationIssue] = []
    extract_root = SCORM_EXTRACTED_DIR / package_id
    extract_root.mkdir(parents=True, exist_ok=True)
    max_entries = 2000
    manifest_name: str | None = None

    try:
        with zipfile.ZipFile(upload_path) as archive:
            members = archive.infolist()
            if len(members) > max_entries:
                raise HTTPException(status_code=400, detail=f"SCORM zip has too many entries ({len(members)} > {max_entries}).")
            for member in members:
                normalized = sanitize_scorm_member_name(member.filename)
                if not normalized.parts:
                    continue
                if member.is_dir():
                    (extract_root / Path(*normalized.parts)).mkdir(parents=True, exist_ok=True)
                    continue
                destination = extract_root / Path(*normalized.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member, 'r') as source, destination.open('wb') as target:
                    shutil.copyfileobj(source, target)
                if normalized.name.lower() == 'imsmanifest.xml' and manifest_name is None:
                    manifest_name = str(Path(*normalized.parts))
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail='Uploaded file is not a valid zip archive.') from exc

    if manifest_name is None:
        issues.append(ScormValidationIssue(level='error', message='imsmanifest.xml is missing.'))
        manifest_root = None
    else:
        manifest_path = extract_root / manifest_name
        try:
            manifest_root = ET.fromstring(manifest_path.read_text(encoding='utf-8'))
        except (ET.ParseError, UnicodeDecodeError) as exc:
            issues.append(ScormValidationIssue(level='error', message=f'imsmanifest.xml could not be parsed: {exc}'))
            manifest_root = None

    title = upload_path.stem
    launch_path: str | None = None
    scorm_version: ScormVersion | None = None
    if manifest_root is not None:
        scorm_version = detect_scorm_version(manifest_root)
        title_node = next((node for node in manifest_root.iter() if node.tag.split('}')[-1].lower() == 'title' and (node.text or '').strip()), None)
        if title_node is not None:
            title = (title_node.text or '').strip()
        resource_map: dict[str, str] = {}
        for node in manifest_root.iter():
            if node.tag.split('}')[-1].lower() != 'resource':
                continue
            identifier = (node.attrib.get('identifier') or '').strip()
            href = (node.attrib.get('href') or '').strip()
            if identifier and href:
                resource_map[identifier] = href
        first_item_identifier = None
        for node in manifest_root.iter():
            if node.tag.split('}')[-1].lower() != 'item':
                continue
            identifierref = (node.attrib.get('identifierref') or '').strip()
            if identifierref:
                first_item_identifier = identifierref
                break
        if first_item_identifier and first_item_identifier in resource_map:
            launch_path = resource_map[first_item_identifier]
        elif resource_map:
            launch_path = next(iter(resource_map.values()))
        else:
            candidate = extract_root / 'index.html'
            if candidate.exists():
                launch_path = 'index.html'

        if scorm_version is None:
            issues.append(ScormValidationIssue(level='warning', message='Could not confidently detect SCORM version from manifest.'))
        if not launch_path:
            issues.append(ScormValidationIssue(level='error', message='No launchable SCO resource href found in manifest.'))
        elif not (extract_root / Path(*sanitize_scorm_member_name(launch_path).parts)).exists():
            issues.append(ScormValidationIssue(level='error', message=f'Launch resource is missing from archive: {launch_path}'))

    now = datetime.now(timezone.utc).isoformat()
    valid = not any(issue.level == 'error' for issue in issues)
    viewer_url = f"/scorm/viewer/{package_id}" if valid and launch_path else None
    return ScormPackageDetail(
        package_id=package_id,
        file_name=upload_path.name,
        scorm_version=scorm_version,
        title=title,
        launch_path=launch_path,
        valid=valid,
        uploaded_at=now,
        updated_at=now,
        issue_count=len(issues),
        extracted_dir=str(extract_root),
        issues=issues,
        viewer_url=viewer_url,
        attempts=[],
    )


def get_scorm_package_or_404(package_id: str) -> ScormPackageDetail:
    prune_expired_scorm_data(force=True)
    for package in scorm_packages:
        if package.package_id == package_id:
            return package
    raise HTTPException(status_code=404, detail=f"SCORM package not found: {package_id}")


def get_scorm_attempt_or_404(attempt_id: str) -> ScormAttemptSummary:
    attempt = scorm_attempts.get(attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404, detail=f"SCORM attempt not found: {attempt_id}")
    return attempt


def summarize_scorm_attempt(attempt: ScormAttemptSummary) -> ScormAttemptSummary:
    return attempt


def ensure_attempt_runtime(attempt_id: str) -> dict[str, str]:
    runtime = scorm_runtime_store.get(attempt_id)
    if runtime is None:
        runtime = {}
        scorm_runtime_store[attempt_id] = runtime
    return runtime


def runtime_defaults_for_version(version: ScormVersion | None) -> dict[str, str]:
    if version == 'scorm2004':
        return {
            'cmi.completion_status': 'not attempted',
            'cmi.success_status': 'unknown',
            'cmi.score.raw': '',
            'cmi.location': '',
            'cmi.suspend_data': '',
        }
    return {
        'cmi.core.lesson_status': 'not attempted',
        'cmi.core.score.raw': '',
        'cmi.core.lesson_location': '',
        'cmi.suspend_data': '',
    }


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
        "probing": "Media probe placeholder",
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


def sanitize_output_filename(raw_value: str, fallback: str) -> str:
    compact = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '-', raw_value.strip())
    compact = re.sub(r'\s+', ' ', compact).strip().strip('.')
    return compact or fallback


def resolve_transcript_file_name(
    job: JobDetail,
    format_name: Literal["srt", "vtt"],
    output_filename: str | None = None,
) -> str:
    default_stem = Path(job.media_metadata.file_name).stem if job.media_metadata and job.media_metadata.file_name.strip() else job.job_id
    safe_stem = sanitize_output_filename(default_stem, job.job_id)
    if not output_filename or not output_filename.strip():
        return f"{safe_stem}.{format_name}"
    candidate = sanitize_output_filename(output_filename, safe_stem)
    suffix = Path(candidate).suffix.lower()
    if suffix == f".{format_name}":
        return candidate
    if suffix in {".srt", ".vtt", ".zip", ".mp4"}:
        candidate = Path(candidate).stem
    return f"{sanitize_output_filename(candidate, safe_stem)}.{format_name}"


def build_transcript_artifact(
    job: JobDetail, format_name: Literal["srt", "vtt"], output_filename: str | None = None
) -> GeneratedArtifact:
    ensure_job_artifact_dir(job.job_id)
    timestamp_fragment = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    file_name = resolve_transcript_file_name(job, format_name, output_filename)
    artifact_path = ensure_job_artifact_dir(job.job_id) / f"{timestamp_fragment}-{file_name}"
    content = (
        render_srt(job.transcript_segments)
        if format_name == "srt"
        else render_vtt(job.transcript_segments)
    )
    artifact_path.write_text(content, encoding="utf-8")
    now = datetime.now(timezone.utc).isoformat()
    artifact_kind: Literal["transcript-srt", "transcript-vtt"] = (
        "transcript-srt" if format_name == "srt" else "transcript-vtt"
    )
    artifact_id = make_artifact_id(job.job_id, len(job.artifacts) + 1)
    return GeneratedArtifact(
        artifact_id=artifact_id,
        artifact_kind=artifact_kind,
        format_name=format_name,
        file_name=file_name,
        artifact_path=to_artifact_relative_path(artifact_path),
        download_url=build_artifact_download_url(job.job_id, artifact_id),
        size_bytes=artifact_path.stat().st_size,
        transcript_segment_count=len(job.transcript_segments),
        created_at=now,
    )


def normalize_package_base_name(raw_value: str) -> str:
    compact = sanitize_output_filename(raw_value, "course")
    compact = re.sub(r"\s+", " ", compact).strip()
    return compact or "course"


def package_archive_name(title: str, format_name: Literal["scorm12", "scorm2004", "aicc", "xapi", "cmi5"]) -> str:
    format_labels = {
        "scorm12": "SCORM-1.2",
        "scorm2004": "SCORM-2004",
        "aicc": "AICC",
        "xapi": "xAPI",
        "cmi5": "cmi5",
    }
    base = normalize_package_base_name(title)
    return f"{base}-{format_labels[format_name]}.zip"


def package_title(job: JobDetail) -> str:
    if job.media_metadata and job.media_metadata.file_name.strip():
        title = Path(job.media_metadata.file_name).stem.strip()
        if title:
            return title
    media_stem = Path(job.media_path).stem.strip()
    return media_stem if media_stem else job.job_id


def resolve_package_title(job: JobDetail, output_filename: str | None = None) -> str:
    default_title = package_title(job)
    if not output_filename or not output_filename.strip():
        return normalize_package_base_name(default_title)
    candidate = sanitize_output_filename(output_filename, default_title)
    lower_candidate = candidate.lower()
    for suffix in ["-scorm-1.2", "-scorm-2004", "-aicc", "-xapi", "-cmi5"]:
        if lower_candidate.endswith(suffix):
            candidate = candidate[: -len(suffix)]
            break
    if Path(candidate).suffix.lower() == ".zip":
        candidate = Path(candidate).stem
    return normalize_package_base_name(candidate)


def package_index_html(
    title: str,
    format_name: str,
    media_archive_path: str,
    activity_id: str,
) -> str:
    safe_title = escape(title, quote=True)
    caption_vtt_path = f"captions/{title}.vtt"
    caption_srt_path = f"captions/{title}.srt"
    config = json.dumps(
        {
            "title": title,
            "format": format_name,
            "mediaPath": media_archive_path,
            "captionVtt": caption_vtt_path,
            "captionSrt": caption_srt_path,
            "activityId": activity_id,
        }
    )
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '  <meta charset="utf-8">',
            '  <meta name="viewport" content="width=device-width, initial-scale=1">',
            f"  <title>{safe_title}</title>",
            "  <style>",
            "    :root { color-scheme: dark; font-family: Arial, sans-serif; }",
            "    body { margin: 0; background: #0f172a; color: #e2e8f0; }",
            "    main { max-width: 1040px; margin: 0 auto; padding: 24px; display: grid; gap: 16px; }",
            "    .card { background: #111827; border: 1px solid #334155; border-radius: 12px; padding: 16px; }",
            "    .control-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-bottom: 12px; }",
            "    .control-group { display: flex; align-items: center; gap: 8px; }",
            "    .control-label { color: #94a3b8; font-size: 0.85rem; }",
            "    .control-status { color: #94a3b8; font-size: 0.85rem; }",
            "    .btn { font-size: 12px; padding: 6px 12px; border-radius: 999px; border: 1px solid #475569; background: #0f172a; color: #e2e8f0; cursor: pointer; }",
            "    .btn:hover { background: #1e293b; }",
            "    .range-input { accent-color: #60a5fa; }",
            "    #seek-bar { width: min(320px, 40vw); }",
            "    #volume-bar { width: 88px; }",
            "    #playback-rate { background: #0f172a; color: #e2e8f0; border: 1px solid #475569; border-radius: 8px; padding: 5px 8px; }",
            "    video { width: 100%; max-height: 70vh; background: black; border-radius: 10px; }",
            "    .meta { color: #94a3b8; font-size: 0.92rem; }",
            "    .status { white-space: pre-wrap; font-family: ui-monospace, monospace; background: #020617; padding: 12px; border-radius: 10px; }",
            "  </style>",
            "</head>",
            "<body>",
            "  <main>",
            '    <section class="card">',
            f"      <h1>{safe_title}</h1>",
            f"      <p class=\"meta\">Package format: {escape(format_name, quote=True)}</p>",
            '      <div class="control-bar">',
            '        <div class="control-group">',
            '          <button type="button" class="btn" id="cc-toggle">CC On</button>',
            '          <button type="button" class="btn" id="seek-back">-10s</button>',
            '          <button type="button" class="btn" id="seek-forward">+10s</button>',
            '        </div>',
            '        <div class="control-group">',
            '          <span class="control-label">Seek</span>',
            '          <input id="seek-bar" class="range-input" type="range" min="0" max="1000" value="0" step="1">',
            '          <span id="time-readout" class="control-status">0:00 / 0:00</span>',
            '        </div>',
            '        <div class="control-group">',
            '          <button type="button" class="btn" id="mute-toggle">Mute</button>',
            '          <span class="control-label">Vol</span>',
            '          <input id="volume-bar" class="range-input" type="range" min="0" max="1" value="1" step="0.05">',
            '        </div>',
            '        <div class="control-group">',
            '          <span class="control-label">Speed</span>',
            '          <select id="playback-rate">',
            '            <option value="0.75">0.75×</option>',
            '            <option value="1" selected>1×</option>',
            '            <option value="1.25">1.25×</option>',
            '            <option value="1.5">1.5×</option>',
            '            <option value="1.75">1.75×</option>',
            '            <option value="2">2×</option>',
            '          </select>',
            '        </div>',
            '        <span id="control-status" class="control-status">Captions and wrapper controls ready</span>',
            '      </div>',
            "      <video id=\"course-video\" controls preload=\"metadata\" playsinline>",
            f"        <source src=\"{escape(media_archive_path, quote=True)}\" type=\"video/mp4\" />",
            f'        <track kind="subtitles" srclang="en" label="English" src="{escape(caption_vtt_path, quote=True)}" default />',
            "      </video>",
            "    </section>",
            '    <section class="card">',
            '      <h2>Runtime status</h2>',
            '      <div id="runtime-status" class="status">Preparing package runtime…</div>',
            "    </section>",
            "  </main>",
            f"  <script>window.SUBTITLE_WORKSTATION_PACKAGE = {config};</script>",
            '  <script src="runtime.js"></script>',
            "</body>",
            "</html>",
            "",
        ]
    )


def scorm_manifest(
    format_name: Literal["scorm12", "scorm2004"], title: str, media_archive_path: str
) -> str:
    safe_title = escape(title, quote=True)
    safe_media = escape(media_archive_path, quote=True)
    caption_vtt = escape(f"captions/{title}.vtt", quote=True)
    caption_srt = escape(f"captions/{title}.srt", quote=True)
    shared_files = (
        '<file href="index.html"/><file href="runtime.js"/>'
        f'<file href="{caption_vtt}"/><file href="{caption_srt}"/>'
        f'<file href="{safe_media}"/>'
    )
    if format_name == "scorm12":
        return "\n".join(
            [
                '<?xml version="1.0" encoding="UTF-8"?>',
                '<manifest identifier="MANIFEST-1" version="1.0" xmlns="http://www.imsproject.org/xsd/imscp_rootv1p1p2" xmlns:adlcp="http://www.adlnet.org/xsd/adlcp_rootv1p2">',
                '  <metadata><schema>ADL SCORM</schema><schemaversion>1.2</schemaversion></metadata>',
                f'  <organizations default="ORG-1"><organization identifier="ORG-1"><title>{safe_title}</title><item identifier="ITEM-1" identifierref="RES-1"><title>{safe_title}</title></item></organization></organizations>',
                f'  <resources><resource identifier="RES-1" type="webcontent" adlcp:scormtype="sco" href="index.html">{shared_files}</resource></resources>',
                '</manifest>',
                '',
            ]
        )
    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<manifest identifier="MANIFEST-1" version="1.0" xmlns="http://www.imsglobal.org/xsd/imscp_v1p1" xmlns:adlcp="http://www.adlnet.org/xsd/adlcp_v1p3">',
            '  <metadata><schema>ADL SCORM</schema><schemaversion>2004 4th Edition</schemaversion></metadata>',
            f'  <organizations default="ORG-1"><organization identifier="ORG-1"><title>{safe_title}</title><item identifier="ITEM-1" identifierref="RES-1"><title>{safe_title}</title></item></organization></organizations>',
            f'  <resources><resource identifier="RES-1" type="webcontent" adlcp:scormType="sco" href="index.html">{shared_files}</resource></resources>',
            '</manifest>',
            '',
        ]
    )


def xapi_tincan_xml(title: str, activity_id: str) -> str:
    safe_title = escape(title, quote=True)
    safe_activity = escape(activity_id, quote=True)
    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<tincan xmlns="http://projecttincan.com/tincan.xsd">',
            '  <activities>',
            f'    <activity id="{safe_activity}">',
            f'      <name lang="en-US">{safe_title}</name>',
            '      <description lang="en-US">Video lesson package generated from Subtitle Workstation state.</description>',
            '      <type>http://adlnet.gov/expapi/activities/lesson</type>',
            '    </activity>',
            '  </activities>',
            '  <launch default="index.html"/>',
            '</tincan>',
            '',
        ]
    )


def cmi5_xml(title: str, activity_id: str) -> str:
    safe_title = escape(title, quote=True)
    safe_activity = escape(activity_id, quote=True)
    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<courseStructure xmlns="https://w3id.org/xapi/profiles/cmi5/v1/CourseStructure.xsd">',
            f'  <course id="{safe_activity}">',
            f'    <title><langstring lang="en-US">{safe_title}</langstring></title>',
            '    <description><langstring lang="en-US">Video lesson package generated from Subtitle Workstation state.</langstring></description>',
            '    <au id="AU-1">',
            f'      <title><langstring lang="en-US">{safe_title}</langstring></title>',
            '      <description><langstring lang="en-US">Primary video activity</langstring></description>',
            '      <launchMethod>AnyWindow</launchMethod>',
            '      <masteryScore>0</masteryScore>',
            '      <moveOn>Completed</moveOn>',
            '      <url>index.html</url>',
            '    </au>',
            '  </course>',
            '</courseStructure>',
            '',
        ]
    )


def aicc_crs(title: str, base: str) -> str:
    return "\n".join([
        '[Course]',
        f'Course_ID={base}',
        f'Course_Title={title}',
        'Version=4.0',
        'Level=1',
        '',
    ])


def aicc_au(title: str, base: str) -> str:
    return "\n".join(
        [
            '[AU]',
            f'System_ID={base}-au1',
            f'Title={title}',
            'File_Name=index.html',
            'Core_Vendor=SubtitleWorkstation',
            'Web_Launch=yes',
            'Max_Time_Allowed=00:00:00',
            'Time_Limit_Action=continue,no message',
            '',
        ]
    )


def aicc_des(title: str, base: str) -> str:
    return "\n".join(
        [
            '[Descriptor]',
            f'System_ID={base}-des1',
            f'Title={title} Assignment',
            'Description=Video lesson package generated from Subtitle Workstation state.',
            'Prerequisites=',
            'Objectives=',
            'Max_Score=100',
            'Passing_Score=0',
            '',
        ]
    )


def aicc_cst(base: str) -> str:
    return "\n".join(
        [
            '[Course_Structure]',
            'Block=1',
            f'Member={base}-des1',
            f'Member={base}-au1',
            '',
            '[Assign]',
            f'Descriptor={base}-des1',
            f'Assignable_Unit={base}-au1',
            'Relationship=credit',
            '',
        ]
    )


def package_runtime_js() -> str:
    return '''
(function () {
  const config = window.SUBTITLE_WORKSTATION_PACKAGE || {};
  const video = document.getElementById("course-video");
  const statusNode = document.getElementById("runtime-status");
  const ccToggle = document.getElementById("cc-toggle");
  const seekBackBtn = document.getElementById("seek-back");
  const seekForwardBtn = document.getElementById("seek-forward");
  const seekBar = document.getElementById("seek-bar");
  const timeReadout = document.getElementById("time-readout");
  const muteToggle = document.getElementById("mute-toggle");
  const volumeBar = document.getElementById("volume-bar");
  const playbackRate = document.getElementById("playback-rate");
  const controlStatus = document.getElementById("control-status");
  const state = { completed: false, started: false, initialized: false, terminated: false, startMs: Date.now(), lastCommitMs: 0 };

  function log(message) {
    console.log("[subtitle-workstation-package]", message);
    if (statusNode) statusNode.textContent = String(message);
    try {
      if (window.parent && window.parent !== window) {
        window.parent.postMessage({ source: "subtitle-workstation-runtime", message: String(message) }, "*");
      }
    } catch {}
  }
  log("runtime.js booted");
  function q(name) { return new URLSearchParams(window.location.search).get(name); }
  function parseJsonParam(name) { const raw = q(name); if (!raw) return null; try { return JSON.parse(raw); } catch { return null; } }
  function durationWatched() { return Math.max(0, Math.round((Date.now() - state.startMs) / 1000)); }
  function progressMeasure() { if (!video || !video.duration || !Number.isFinite(video.duration) || video.duration <= 0) return 0; return Math.max(0, Math.min(1, video.currentTime / video.duration)); }
  function scoreRaw() { return Math.round(progressMeasure() * 100); }
  function fmtTime(seconds) { if (!Number.isFinite(seconds) || seconds < 0) return "0:00"; const rounded = Math.floor(seconds); const h = Math.floor(rounded / 3600); const m = Math.floor((rounded % 3600) / 60); const s = rounded % 60; return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`; }
  function scorm12Time(seconds) { const s = Math.max(0, Math.round(seconds)); const h = String(Math.floor(s / 3600)).padStart(2, "0"); const m = String(Math.floor((s % 3600) / 60)).padStart(2, "0"); const sec = String(s % 60).padStart(2, "0"); return `${h}:${m}:${sec}`; }
  function isoDuration(seconds) { const s = Math.max(0, Math.round(seconds)); const h = Math.floor(s / 3600); const m = Math.floor((s % 3600) / 60); const sec = s % 60; return `PT${h ? `${h}H` : ""}${m ? `${m}M` : ""}${sec || (!h && !m) ? `${sec}S` : ""}`; }
  function findApi(start, name) { let win = start; let depth = 0; while (win && depth < 10) { try { if (win[name]) return win[name]; if (win.parent && win.parent !== win) win = win.parent; else break; } catch { break; } depth += 1; } try { if (window.opener && window.opener[name]) return window.opener[name]; } catch {} return null; }
  function captionTracks() { return video && video.textTracks ? Array.from(video.textTracks) : []; }
  function setControlStatus(message) { if (controlStatus) controlStatus.textContent = String(message); }
  function captionsEnabled() { const tracks = captionTracks(); return tracks.some(track => track.mode === "showing"); }
  function applyCaptions(enabled) { const tracks = captionTracks(); if (!tracks.length) { if (ccToggle) ccToggle.textContent = "CC N/A"; setControlStatus("No caption track found"); return; } tracks.forEach(track => { track.mode = enabled ? "showing" : "disabled"; }); if (ccToggle) ccToggle.textContent = enabled ? "CC On" : "CC Off"; setControlStatus(enabled ? `Captions on (${tracks.length} track)` : "Captions off"); }
  function syncControls() {
    if (!video) return;
    if (seekBar) seekBar.value = video.duration && Number.isFinite(video.duration) ? String(Math.round((video.currentTime / video.duration) * 1000)) : "0";
    if (timeReadout) timeReadout.textContent = `${fmtTime(video.currentTime || 0)} / ${fmtTime(video.duration || 0)}`;
    if (muteToggle) muteToggle.textContent = video.muted || video.volume === 0 ? "Unmute" : "Mute";
    if (volumeBar) volumeBar.value = String(video.muted ? 0 : video.volume);
    if (playbackRate) playbackRate.value = String(video.playbackRate || 1);
    if (ccToggle && captionTracks().length) ccToggle.textContent = captionsEnabled() ? "CC On" : "CC Off";
  }

  const directRuntime = {
    attemptId: q("attempt_id"),
    apiBase: q("api_base"),
    version: q("scorm_version") || config.format,
    async call(method, payload) {
      if (!this.attemptId || !this.apiBase) { log(`Direct runtime ${method} skipped: missing attempt_id/api_base`); return null; }
      try {
        log(`Direct runtime ${method} -> ${this.apiBase}/scorm/attempts/${this.attemptId}/runtime/${method}`);
        const response = await fetch(`${this.apiBase}/scorm/attempts/${this.attemptId}/runtime/${method}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload || {}),
          keepalive: true,
        });
        const payloadOut = await response.json();
        log(`Direct runtime ${method} <- ${response.status}`);
        return payloadOut;
      } catch (error) {
        log(`Direct runtime ${method} failed: ${error}`);
        return null;
      }
    },
    async set(element, value) {
      return this.call("set", { element, value });
    },
    async init() {
      return this.call("initialize", {});
    },
    async commit() {
      return this.call("commit", {});
    },
    async terminate() {
      return this.call("terminate", {});
    },
  };

  const scorm = {
    api12: null,
    api2004: null,
    async init() {
      if (config.format === "scorm12") {
        this.api12 = findApi(window, "API");
        log(`SCORM 1.2 API ${this.api12 ? 'found' : 'not found'}`);
        if (this.api12) {
          this.api12.LMSInitialize("");
          this.api12.LMSSetValue("cmi.core.lesson_status", "incomplete");
          this.api12.LMSCommit("");
          state.initialized = true;
          log("SCORM 1.2 initialized");
          return;
        }
        if (directRuntime.attemptId && directRuntime.apiBase) {
          log('SCORM 1.2 entering direct runtime fallback');
          await directRuntime.init();
          await directRuntime.set("cmi.core.lesson_status", "incomplete");
          await directRuntime.commit();
          state.initialized = true;
          log("SCORM 1.2 initialized via direct runtime fallback");
          return;
        }
        log("SCORM 1.2 API not found");
      } else if (config.format === "scorm2004") {
        this.api2004 = findApi(window, "API_1484_11");
        log(`SCORM 2004 API ${this.api2004 ? 'found' : 'not found'}`);
        if (this.api2004) {
          this.api2004.Initialize("");
          this.api2004.SetValue("cmi.completion_status", "incomplete");
          this.api2004.SetValue("cmi.success_status", "unknown");
          this.api2004.Commit("");
          state.initialized = true;
          log("SCORM 2004 initialized");
          return;
        }
        if (directRuntime.attemptId && directRuntime.apiBase) {
          await directRuntime.init();
          await directRuntime.set("cmi.completion_status", "incomplete");
          await directRuntime.set("cmi.success_status", "unknown");
          await directRuntime.commit();
          state.initialized = true;
          log("SCORM 2004 initialized via direct runtime fallback");
          return;
        }
        log("SCORM 2004 API not found");
      }
    },
    async commit(forceComplete) {
      if (this.api12) {
        this.api12.LMSSetValue("cmi.core.lesson_location", String(Math.round(video.currentTime || 0)));
        this.api12.LMSSetValue("cmi.core.score.raw", String(scoreRaw()));
        this.api12.LMSSetValue("cmi.core.session_time", scorm12Time(durationWatched()));
        this.api12.LMSSetValue("cmi.suspend_data", JSON.stringify({ currentTime: video.currentTime || 0 }));
        this.api12.LMSSetValue("cmi.core.lesson_status", forceComplete ? "completed" : "incomplete");
        this.api12.LMSCommit("");
      } else if (this.api2004) {
        this.api2004.SetValue("cmi.location", String(Math.round(video.currentTime || 0)));
        this.api2004.SetValue("cmi.progress_measure", String(progressMeasure().toFixed(4)));
        this.api2004.SetValue("cmi.score.raw", String(scoreRaw()));
        this.api2004.SetValue("cmi.score.scaled", String(progressMeasure().toFixed(4)));
        this.api2004.SetValue("cmi.session_time", isoDuration(durationWatched()));
        this.api2004.SetValue("cmi.suspend_data", JSON.stringify({ currentTime: video.currentTime || 0 }));
        this.api2004.SetValue("cmi.completion_status", forceComplete ? "completed" : "incomplete");
        if (forceComplete) this.api2004.SetValue("cmi.success_status", "passed");
        this.api2004.Commit("");
      } else if (directRuntime.attemptId && directRuntime.apiBase) {
        if (config.format === "scorm2004") {
          await directRuntime.set("cmi.location", String(Math.round(video.currentTime || 0)));
          await directRuntime.set("cmi.score.raw", String(scoreRaw()));
          await directRuntime.set("cmi.suspend_data", JSON.stringify({ currentTime: video.currentTime || 0 }));
          await directRuntime.set("cmi.completion_status", forceComplete ? "completed" : "incomplete");
          if (forceComplete) await directRuntime.set("cmi.success_status", "passed");
        } else {
          await directRuntime.set("cmi.core.lesson_location", String(Math.round(video.currentTime || 0)));
          await directRuntime.set("cmi.core.score.raw", String(scoreRaw()));
          await directRuntime.set("cmi.suspend_data", JSON.stringify({ currentTime: video.currentTime || 0 }));
          await directRuntime.set("cmi.core.lesson_status", forceComplete ? "completed" : "incomplete");
        }
        await directRuntime.commit();
      }
    },
    async terminate() {
      if (state.terminated) return;
      await this.commit(state.completed);
      if (this.api12) this.api12.LMSFinish("");
      if (this.api2004) this.api2004.Terminate("");
      else if (directRuntime.attemptId && directRuntime.apiBase) await directRuntime.terminate();
      state.terminated = true;
    }
  };

  const aicc = {
    get sid() { return q("aicc_sid"); },
    get url() { return q("aicc_url"); },
    async send(command, aiccData) {
      if (!this.sid || !this.url) { log("AICC launch parameters not present"); return; }
      const body = new URLSearchParams({ command, version: "4.0", session_id: this.sid, AICC_SID: this.sid, AICC_Data: aiccData || "" });
      await fetch(this.url, { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: body.toString(), keepalive: true });
    },
    async init() { if (config.format !== "aicc") return; state.initialized = true; await this.send("GetParam", ""); log("AICC initialized"); },
    async commit(forceComplete) {
      if (config.format !== "aicc" || !video) return;
      const payload = ["[Core]", `Lesson_Location=${Math.round(video.currentTime || 0)}`, `Lesson_Status=${forceComplete ? "completed" : "incomplete"}`, `Score=${scoreRaw()}`, "", "[Core_Lesson]", JSON.stringify({ currentTime: video.currentTime || 0 }), ""].join("\\n");
      await this.send("PutParam", payload);
    }
  };

  const xapi = {
    endpoint: null, auth: null, actor: null, registration: null, activityId: null,
    async init() {
      if (config.format !== "xapi" && config.format !== "cmi5") return;
      this.endpoint = q("endpoint");
      this.auth = q("auth");
      this.actor = parseJsonParam("actor");
      this.registration = q("registration");
      this.activityId = q("activityId") || config.activityId;
      const fetchUrl = q("fetch");
      if (fetchUrl && !this.auth) {
        try {
          const response = await fetch(fetchUrl, { credentials: "include" });
          const payload = await response.json();
          this.auth = payload.auth || payload.authorization || payload.token || this.auth;
          this.endpoint = payload.endpoint || this.endpoint;
          this.actor = payload.actor || this.actor;
          this.registration = payload.registration || this.registration;
          this.activityId = payload.activityId || this.activityId;
        } catch (error) {
          console.warn("Failed to resolve cmi5/xAPI fetch launch data", error);
        }
      }
      if (!this.endpoint || !this.actor || !this.activityId) { log(`${config.format} launch parameters not present`); return; }
      state.initialized = true;
      await this.sendStatement("http://adlnet.gov/expapi/verbs/initialized");
      log(`${config.format} initialized`);
    },
    async sendStatement(verbId, result) {
      if (!this.endpoint || !this.actor || !this.activityId) return;
      const statement = {
        actor: this.actor,
        verb: { id: verbId, display: { "en-US": verbId.split("/").slice(-1)[0] } },
        object: { id: this.activityId, definition: { type: "http://adlnet.gov/expapi/activities/lesson", name: { "en-US": config.title || "Video lesson" } } },
        context: { registration: this.registration || undefined, contextActivities: config.format === "cmi5" ? { category: [{ id: "https://w3id.org/xapi/cmi5/context/categories/cmi5" }] } : undefined },
        result: result || undefined,
        timestamp: new Date().toISOString(),
      };
      const headers = { "Content-Type": "application/json" };
      if (this.auth) headers.Authorization = this.auth;
      await fetch(this.endpoint.replace(/\/$/, "") + "/statements", { method: "POST", headers, body: JSON.stringify(statement), keepalive: true });
    },
    async commit(forceComplete) {
      if (!state.initialized) return;
      await this.sendStatement(forceComplete ? "http://adlnet.gov/expapi/verbs/completed" : "http://adlnet.gov/expapi/verbs/progressed", { completion: !!forceComplete, score: { raw: scoreRaw(), scaled: progressMeasure() }, duration: isoDuration(durationWatched()), extensions: { "https://subtitle-workstation.ai/extensions/current-time": Math.round(video.currentTime || 0) } });
      if (forceComplete && config.format === "cmi5") {
        await this.sendStatement("http://adlnet.gov/expapi/verbs/passed", { success: true, score: { raw: scoreRaw(), scaled: progressMeasure() } });
      }
    },
    async terminate() {
      if (state.terminated || !state.initialized) return;
      await this.sendStatement("http://adlnet.gov/expapi/verbs/terminated", { duration: isoDuration(durationWatched()) });
      state.terminated = true;
    }
  };

  async function initialize() {
    log('initialize() called');
    if (!video) { log("Video element not found"); return; }
    log('Video element found');
    if (ccToggle) ccToggle.addEventListener("click", () => applyCaptions(!captionsEnabled()));
    if (seekBackBtn) seekBackBtn.addEventListener("click", () => { video.currentTime = Math.max(0, (video.currentTime || 0) - 10); syncControls(); });
    if (seekForwardBtn) seekForwardBtn.addEventListener("click", () => { const duration = Number.isFinite(video.duration) ? video.duration : Infinity; video.currentTime = Math.min(duration, (video.currentTime || 0) + 10); syncControls(); });
    if (seekBar) seekBar.addEventListener("input", (event) => { if (!video.duration || !Number.isFinite(video.duration)) return; const ratio = Number(event.target.value || 0) / 1000; video.currentTime = video.duration * ratio; syncControls(); });
    if (muteToggle) muteToggle.addEventListener("click", () => { video.muted = !video.muted; syncControls(); });
    if (volumeBar) volumeBar.addEventListener("input", (event) => { const volume = Math.max(0, Math.min(1, Number(event.target.value || 0))); video.volume = volume; video.muted = volume === 0; syncControls(); });
    if (playbackRate) playbackRate.addEventListener("change", (event) => { video.playbackRate = Number(event.target.value || 1); syncControls(); });
    video.addEventListener("loadedmetadata", () => { applyCaptions(true); syncControls(); });
    video.addEventListener("timeupdate", syncControls);
    video.addEventListener("volumechange", syncControls);
    video.addEventListener("ratechange", syncControls);
    video.addEventListener("seeked", syncControls);
    syncControls();
    if (config.format === "scorm12" || config.format === "scorm2004") await scorm.init();
    if (config.format === "aicc") await aicc.init();
    if (config.format === "xapi" || config.format === "cmi5") await xapi.init();
    log(`Ready: ${config.format} package loaded`);
  }

  async function commit(forceComplete) {
    if (!video) return;
    state.completed = state.completed || !!forceComplete;
    if (config.format === "scorm12" || config.format === "scorm2004") await scorm.commit(forceComplete);
    if (config.format === "aicc") await aicc.commit(forceComplete);
    if (config.format === "xapi" || config.format === "cmi5") await xapi.commit(forceComplete);
    state.lastCommitMs = Date.now();
    log(`Progress saved at ${Math.round(video.currentTime || 0)}s (${Math.round(progressMeasure() * 100)}%)`);
  }

  if (video) {
    video.addEventListener("play", async () => {
      if (!state.started) {
        state.started = true;
        if (config.format === "xapi") await xapi.sendStatement("http://adlnet.gov/expapi/verbs/experienced");
      }
    });
    video.addEventListener("timeupdate", () => { if (Date.now() - state.lastCommitMs > 15000) void commit(false); });
    video.addEventListener("pause", () => void commit(false));
    video.addEventListener("ended", () => void commit(true));
  }

  window.addEventListener("beforeunload", () => {
    if (config.format === "scorm12" || config.format === "scorm2004") void scorm.terminate();
    if (config.format === "xapi" || config.format === "cmi5") void xapi.terminate();
    if (config.format === "aicc") void aicc.commit(state.completed);
  });

  void initialize();
})();
'''.strip() + "\n"


def build_package_artifact(
    job: JobDetail,
    format_name: Literal["scorm12", "scorm2004", "aicc", "xapi", "cmi5"],
    output_filename: str | None = None,
) -> GeneratedArtifact:
    artifact_dir = ensure_job_artifact_dir(job.job_id)
    title = resolve_package_title(job, output_filename)
    now = datetime.now(timezone.utc).isoformat()
    source_media_path = Path(job.media_path).expanduser()
    if not source_media_path.exists() or not source_media_path.is_file():
        raise HTTPException(status_code=400, detail=f"Source media is missing: {source_media_path}")

    output_file_name = package_archive_name(title, format_name)
    timestamp_fragment = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output_path = artifact_dir / f"{timestamp_fragment}-{output_file_name}"
    media_archive_path = f"media/{source_media_path.name}"
    activity_id = f"urn:subtitle-workstation:{normalize_package_base_name(title)}:{format_name}"

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("index.html", package_index_html(title, format_name, media_archive_path, activity_id))
        archive.writestr("runtime.js", package_runtime_js())
        archive.writestr(f"captions/{title}.srt", render_srt(job.transcript_segments))
        archive.writestr(f"captions/{title}.vtt", render_vtt(job.transcript_segments))
        archive.write(source_media_path, arcname=media_archive_path, compress_type=zipfile.ZIP_STORED)
        archive.writestr(
            "media-reference.txt",
            "\n".join(
                [
                    f"job_id={job.job_id}",
                    f"media_path={job.media_path}",
                    "media_included=true",
                    f"media_archive_path={media_archive_path}",
                    "note=Package includes source media and runtime wrapper.",
                    "",
                ]
            ),
        )
        archive.writestr(
            "job-state.json",
            json.dumps(
                {
                    "job_id": job.job_id,
                    "format": format_name,
                    "stage": job.stage,
                    "title": title,
                    "transcription_mode": job.transcription_mode,
                    "transcription_source": job.transcription_source,
                    "timing_source": job.timing_source,
                    "media_path": job.media_path,
                    "media_metadata": job.media_metadata.model_dump() if job.media_metadata else None,
                    "transcript_segment_count": len(job.transcript_segments),
                    "subtitle_tracks": [track.model_dump() for track in job.subtitle_tracks],
                    "generated_at": now,
                },
                indent=2,
            )
            + "\n",
        )

        if format_name in {"scorm12", "scorm2004"}:
            archive.writestr("imsmanifest.xml", scorm_manifest(format_name, title, media_archive_path))
        elif format_name in {"xapi", "cmi5"}:
            archive.writestr("tincan.xml", xapi_tincan_xml(title, activity_id))
            if format_name == "cmi5":
                archive.writestr("cmi5.xml", cmi5_xml(title, activity_id))
        else:
            base = normalize_package_base_name(title)
            archive.writestr(f"{base}.crs", aicc_crs(title, base))
            archive.writestr(f"{base}.au", aicc_au(title, base))
            archive.writestr(f"{base}.des", aicc_des(title, base))
            archive.writestr(f"{base}.cst", aicc_cst(base))

    artifact_id = make_artifact_id(job.job_id, len(job.artifacts) + 1)
    return GeneratedArtifact(
        artifact_id=artifact_id,
        artifact_kind=f"package-{format_name}",
        format_name=format_name,
        file_name=output_file_name,
        artifact_path=to_artifact_relative_path(output_path),
        size_bytes=output_path.stat().st_size,
        transcript_segment_count=len(job.transcript_segments),
        created_at=now,
        download_url=build_artifact_download_url(job.job_id, artifact_id),
    )


def parse_srt_timestamp(raw_value: str) -> float:
    timestamp_value = raw_value.strip().split()[0].replace(",", ".")
    parts = timestamp_value.split(":")
    if len(parts) != 3:
        raise ValueError(f"Invalid SRT timestamp: {raw_value}")
    hours = int(parts[0])
    minutes = int(parts[1])
    seconds = float(parts[2])
    return hours * 3600 + minutes * 60 + seconds


def parse_vtt_timestamp(raw_value: str) -> float:
    timestamp_value = raw_value.strip().split()[0].replace(",", ".")
    parts = timestamp_value.split(":")
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


def normalize_subtitle_match_text(value: str) -> str:
    text_value = re.sub(r"<[^>]+>", " ", value.lower())
    text_value = re.sub(r"[^a-z0-9]+", " ", text_value)
    return re.sub(r"\s+", " ", text_value).strip()


def score_subtitle_text_match(old_text: str, new_text: str) -> float:
    old_normalized = normalize_subtitle_match_text(old_text)
    new_normalized = normalize_subtitle_match_text(new_text)
    if not old_normalized or not new_normalized:
        return 0.0

    ratio = SequenceMatcher(None, old_normalized, new_normalized).ratio()
    old_tokens = set(old_normalized.split())
    new_tokens = set(new_normalized.split())
    if not old_tokens or not new_tokens:
        overlap = 0.0
    else:
        overlap = len(old_tokens & new_tokens) / max(len(old_tokens | new_tokens), 1)
    return round((ratio * 0.7) + (overlap * 0.3), 4)


CORRECTION_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "we",
    "with",
    "you",
}


def subtitle_word_tokens(value: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)?", value)


def subtitle_token_spans(value: str) -> list[tuple[str, int, int]]:
    return [
        (normalize_subtitle_match_text(match.group(0)), match.start(), match.end())
        for match in re.finditer(r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)?", value)
        if normalize_subtitle_match_text(match.group(0))
    ]


def find_token_subsequence(
    haystack: list[str],
    needle: list[str],
    *,
    start_at: int = 0,
) -> int | None:
    if not needle or len(needle) > len(haystack):
        return None
    for index in range(max(start_at, 0), len(haystack) - len(needle) + 1):
        if haystack[index : index + len(needle)] == needle:
            return index
    return None


def subtitle_match_tokens(value: str) -> list[str]:
    return normalize_subtitle_match_text(value).split()


def is_prefix_clipped_match(old_text: str, new_text: str) -> bool:
    """Detect current timing text that only captured the start of an old cue."""
    old_tokens = subtitle_match_tokens(old_text)
    new_tokens = subtitle_match_tokens(new_text)
    if not old_tokens or not new_tokens:
        return False
    if len(old_tokens) <= len(new_tokens):
        return False
    if old_tokens[: len(new_tokens)] != new_tokens:
        return False

    missing_token_count = len(old_tokens) - len(new_tokens)
    if missing_token_count < 2:
        return False

    # One-word starts like "Yes." are common CC cues, but also ambiguous.
    # Only trust them when the old cue is short enough that this is clearly a
    # clipped utterance rather than a broad accidental match.
    if len(new_tokens) == 1:
        return len(old_tokens) <= 8

    return True


def token_jaccard(left_text: str, right_text: str) -> float:
    """Token-set Jaccard similarity in [0, 1] between two subtitle strings."""
    left_tokens = set(subtitle_match_tokens(left_text))
    right_tokens = set(subtitle_match_tokens(right_text))
    if not left_tokens or not right_tokens:
        return 0.0
    union = left_tokens | right_tokens
    if not union:
        return 0.0
    return len(left_tokens & right_tokens) / len(union)


def is_low_confidence_clip_preservable(
    *,
    old_segment: TranscriptSegment,
    new_segment: TranscriptSegment,
    confidence: float,
    threshold: float,
) -> tuple[bool, str | None]:
    """When the alignment engine reports low confidence, decide whether the
    previously edited VTT text is still safe to keep.

    The current-timing text can drop, reorder, or partially truncate words
    without the alignment score catching it. Falling back to the new (often
    lower-quality) text in those cases silently discards the user's prior
    edits, which is what the recurring "tail-clipped comment" bug has been.
    Preserving the old text is appropriate when the new text is a subsequence
    of the old one and the timing overlaps meaningfully.
    """
    old_text = old_segment.text or ""
    new_text = new_segment.text or ""
    if not old_text or not new_text:
        return False, None

    old_tokens = subtitle_match_tokens(old_text)
    new_tokens = subtitle_match_tokens(new_text)
    if not old_tokens or not new_tokens:
        return False, None

    overlap = token_jaccard(old_text, new_text)
    if overlap < 0.55:
        return False, None

    start_overlap = abs(old_segment.start_seconds - new_segment.start_seconds) <= 6.0
    end_truncates_old = (old_segment.end_seconds - new_segment.end_seconds) > 0.75
    new_is_subsequence_of_old = (
        find_token_subsequence(old_tokens, new_tokens, start_at=0) is not None
    )
    old_is_subsequence_of_new = (
        find_token_subsequence(new_tokens, old_tokens, start_at=0) is not None
        and len(old_tokens) >= max(4, int(len(new_tokens) * 0.6))
    )

    if start_overlap and end_truncates_old and new_is_subsequence_of_old:
        return (
            True,
            "New timing text was a prefix of the uploaded VTT cue and ended earlier; "
            "the previously edited VTT text was preserved to avoid clipping the tail.",
        )
    if start_overlap and old_is_subsequence_of_new and overlap >= 0.78 and confidence >= max(0.45, threshold - 0.15):
        return (
            True,
            "Upload timing was very close to the previously edited VTT cue; "
            "the uploaded VTT text was preserved verbatim.",
        )
    return False, None


def split_corrected_text_across_timing_segments(
    *,
    corrected_text: str,
    current_texts: list[str],
) -> list[str] | None:
    """Split one old corrected subtitle over several current timing segments.

    WhisperX may split a sentence that the previous edited VTT kept as one cue.
    In that case the old cue should be distributed over the current timing cues,
    not copied wholesale into the first cue while the tail remains duplicated.
    """
    if len(current_texts) < 2:
        return None

    corrected_spans = subtitle_token_spans(corrected_text)
    if not corrected_spans:
        return None
    corrected_tokens = [token for token, _, _ in corrected_spans]

    def chunks_from_matches(matches: list[tuple[int, int]]) -> list[str] | None:
        chunks: list[str] = []
        for index, (match_start, match_end) in enumerate(matches):
            char_start = corrected_spans[match_start][1]
            char_end = corrected_spans[match_end][2]
            if index == 0:
                char_start = 0
            if index == len(matches) - 1:
                char_end = len(corrected_text)
            chunk = corrected_text[char_start:char_end].strip(" \t\r\n-–—")
            if not chunk:
                return None
            chunks.append(chunk)
        return chunks

    def chunks_from_partition_starts(match_starts: list[int]) -> list[str] | None:
        if len(match_starts) != len(current_texts):
            return None
        if match_starts[0] < 0:
            return None
        for previous_start, next_start in zip(match_starts, match_starts[1:]):
            if next_start <= previous_start:
                return None

        chunks: list[str] = []
        for index, match_start in enumerate(match_starts):
            match_end = (
                match_starts[index + 1] - 1
                if index + 1 < len(match_starts)
                else len(corrected_tokens) - 1
            )
            if match_end < match_start:
                return None
            char_start = 0 if index == 0 else corrected_spans[match_start][1]
            char_end = len(corrected_text) if index == len(match_starts) - 1 else corrected_spans[match_end][2]
            chunk = corrected_text[char_start:char_end].strip(" \t\r\n-–—")
            if not chunk:
                return None
            chunks.append(chunk)
        return chunks

    matches: list[tuple[int, int]] = []
    cursor = 0
    for current_text in current_texts:
        current_tokens = [token for token, _, _ in subtitle_token_spans(current_text)]
        if not current_tokens:
            break
        match_start = find_token_subsequence(corrected_tokens, current_tokens, start_at=cursor)
        if match_start is None:
            break
        match_end = match_start + len(current_tokens) - 1
        matches.append((match_start, match_end))
        cursor = match_end + 1

    if (
        len(matches) == len(current_texts)
        and matches[0][0] == 0
        and matches[-1][1] == len(corrected_tokens) - 1
    ):
        return chunks_from_matches(matches)

    overlapping_match_starts: list[int] = []
    cursor = 0
    for current_text in current_texts:
        current_tokens = [token for token, _, _ in subtitle_token_spans(current_text)]
        if not current_tokens:
            overlapping_match_starts = []
            break
        match_start = find_token_subsequence(corrected_tokens, current_tokens, start_at=cursor)
        if match_start is None:
            overlapping_match_starts = []
            break
        overlapping_match_starts.append(match_start)
        cursor = match_start + 1

    if len(overlapping_match_starts) == len(current_texts):
        partition_chunks = chunks_from_partition_starts(overlapping_match_starts)
        if partition_chunks:
            return partition_chunks

    if score_subtitle_text_match(corrected_text, current_texts[0]) < 0.58:
        return None

    trailing_matches: list[tuple[int, int]] = []
    cursor = 0
    for current_text in current_texts[1:]:
        current_tokens = [token for token, _, _ in subtitle_token_spans(current_text)]
        if not current_tokens:
            return None
        match_start = find_token_subsequence(corrected_tokens, current_tokens, start_at=cursor)
        if match_start is None:
            return None
        match_end = match_start + len(current_tokens) - 1
        trailing_matches.append((match_start, match_end))
        cursor = match_end + 1

    if not trailing_matches or trailing_matches[-1][1] != len(corrected_tokens) - 1:
        return None
    first_chunk_end_token = trailing_matches[0][0] - 1
    if first_chunk_end_token < 0:
        return None

    return chunks_from_matches([(0, first_chunk_end_token), *trailing_matches])


def collapse_short_trailing_split_chunks(
    *,
    corrected_text: str,
    chunks: list[str],
    timing_segments: list[TranscriptSegment],
) -> tuple[list[str], list[int]]:
    """Keep tiny tail fragments attached to the cue they complete.

    WhisperX sometimes emits the final word or acronym of a sentence as its
    own very short timing segment. If we preserve that split, playback looks
    like the previous caption was clipped even though the tail technically
    exists in the next cue.
    """
    if len(chunks) < 2 or len(chunks) != len(timing_segments):
        return chunks, []

    collapsed_chunks = list(chunks)
    skipped_indexes: list[int] = []

    while len(collapsed_chunks) - len(skipped_indexes) >= 2:
        tail_index = len(collapsed_chunks) - 1 - len(skipped_indexes)
        tail_text = collapsed_chunks[tail_index]
        tail_tokens = subtitle_match_tokens(tail_text)
        tail_duration = (
            timing_segments[tail_index].end_seconds
            - timing_segments[tail_index].start_seconds
        )
        if len(tail_tokens) > 2:
            break
        if len(tail_text.strip()) > 14:
            break
        if tail_duration > 1.35:
            break

        previous_index = tail_index - 1
        collapsed_chunks[previous_index] = corrected_text
        skipped_indexes.append(tail_index)

    return collapsed_chunks, skipped_indexes


def correction_key(value: str) -> str:
    return normalize_subtitle_match_text(value)


def is_useful_correction_phrase(wrong_text: str, corrected_text: str) -> bool:
    wrong_key = correction_key(wrong_text)
    corrected_key = correction_key(corrected_text)
    if not wrong_key or not corrected_key or wrong_key == corrected_key:
        return False
    wrong_tokens = wrong_key.split()
    corrected_tokens = corrected_key.split()
    if len(wrong_tokens) > 6 or len(corrected_tokens) > 6:
        return False
    if len(wrong_text) > 80 or len(corrected_text) > 80:
        return False
    if all(token in CORRECTION_STOPWORDS for token in wrong_tokens):
        return False
    return True


def make_correction_suggestion(
    *,
    wrong_text: str,
    corrected_text: str,
    confidence: float,
    kind: Literal["phrase", "casing", "llm-candidate"],
    source_segment_id: str | None,
    status: Literal["applied", "suggested"] = "suggested",
    note: str | None = None,
) -> CorrectionSuggestion | None:
    if kind != "casing" and not is_useful_correction_phrase(wrong_text, corrected_text):
        return None
    if kind == "casing" and wrong_text == corrected_text:
        return None
    return CorrectionSuggestion(
        wrong_text=wrong_text,
        corrected_text=corrected_text,
        confidence=round(max(0.0, min(confidence, 1.0)), 3),
        kind=kind,
        source_segment_id=source_segment_id,
        status=status,
        note=note,
    )


def extract_split_join_corrections(
    *,
    wrong_tokens: list[str],
    corrected_tokens: list[str],
    confidence: float,
    source_segment_id: str | None,
) -> list[CorrectionSuggestion]:
    suggestions: list[CorrectionSuggestion] = []
    wrong_index = 0
    corrected_index = 0
    while wrong_index < len(wrong_tokens) and corrected_index < len(corrected_tokens):
        corrected_token = corrected_tokens[corrected_index]
        corrected_key = correction_key(corrected_token)
        if not corrected_key:
            corrected_index += 1
            continue

        found = False
        for end_index in range(wrong_index + 2, min(len(wrong_tokens), wrong_index + 4) + 1):
            wrong_phrase_tokens = wrong_tokens[wrong_index:end_index]
            if "".join(token.lower() for token in wrong_phrase_tokens) != corrected_key:
                continue
            suggestion = make_correction_suggestion(
                wrong_text=" ".join(wrong_phrase_tokens),
                corrected_text=corrected_token,
                confidence=confidence,
                kind="phrase",
                source_segment_id=source_segment_id,
                note="Learned joined-word correction from previous edited VTT.",
            )
            if suggestion:
                suggestions.append(suggestion)
            wrong_index = end_index
            corrected_index += 1
            found = True
            break
        if found:
            continue

        if correction_key(wrong_tokens[wrong_index]) == corrected_key:
            wrong_index += 1
            corrected_index += 1
        else:
            wrong_index += 1

    return suggestions


def extract_correction_suggestions_from_pair(
    *,
    raw_text: str,
    corrected_text: str,
    confidence: float,
    source_segment_id: str | None,
) -> list[CorrectionSuggestion]:
    raw_tokens = subtitle_word_tokens(raw_text)
    corrected_tokens = subtitle_word_tokens(corrected_text)
    raw_lower = [token.lower() for token in raw_tokens]
    corrected_lower = [token.lower() for token in corrected_tokens]
    suggestions: list[CorrectionSuggestion] = []

    matcher = SequenceMatcher(None, raw_lower, corrected_lower)
    for tag, raw_start, raw_end, corrected_start, corrected_end in matcher.get_opcodes():
        wrong_phrase = " ".join(raw_tokens[raw_start:raw_end])
        corrected_phrase = " ".join(corrected_tokens[corrected_start:corrected_end])
        if tag == "equal":
            for raw_token, corrected_token in zip(
                raw_tokens[raw_start:raw_end],
                corrected_tokens[corrected_start:corrected_end],
            ):
                if raw_token != corrected_token and raw_token.lower() == corrected_token.lower():
                    suggestion = make_correction_suggestion(
                        wrong_text=raw_token,
                        corrected_text=corrected_token,
                        confidence=confidence,
                        kind="casing",
                        source_segment_id=source_segment_id,
                        note="Learned casing/style correction from previous edited VTT.",
                    )
                    if suggestion:
                        suggestions.append(suggestion)
            continue

        if tag == "replace":
            suggestions.extend(
                extract_split_join_corrections(
                    wrong_tokens=raw_tokens[raw_start:raw_end],
                    corrected_tokens=corrected_tokens[corrected_start:corrected_end],
                    confidence=confidence,
                    source_segment_id=source_segment_id,
                )
            )
            suggestion = make_correction_suggestion(
                wrong_text=wrong_phrase,
                corrected_text=corrected_phrase,
                confidence=confidence,
                kind="phrase",
                source_segment_id=source_segment_id,
                note="Learned phrase correction from previous edited VTT.",
            )
            if suggestion:
                suggestions.append(suggestion)

    return suggestions


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_json_object_from_text(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        pass

    start = value.find("{")
    end = value.rfind("}")
    if start >= 0 and end > start:
        return json.loads(value[start : end + 1])
    return None


def parse_llm_correction_payload(
    payload: Any,
    *,
    confidence: float,
    source_segment_id: str | None,
) -> list[CorrectionSuggestion]:
    if not isinstance(payload, dict):
        return []
    raw_items = payload.get("corrections")
    if not isinstance(raw_items, list):
        return []

    suggestions: list[CorrectionSuggestion] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        wrong_text = str(item.get("wrong_text") or "").strip()
        corrected_text = str(item.get("corrected_text") or "").strip()
        if not wrong_text or not corrected_text:
            continue
        item_confidence = item.get("confidence", confidence)
        try:
            parsed_confidence = float(item_confidence)
        except (TypeError, ValueError):
            parsed_confidence = confidence
        suggestion = make_correction_suggestion(
            wrong_text=wrong_text,
            corrected_text=corrected_text,
            confidence=min(confidence, parsed_confidence),
            kind="llm-candidate",
            source_segment_id=source_segment_id,
            note="Claude Haiku suggested this correction from the uploaded VTT edits.",
        )
        if suggestion:
            suggestions.append(suggestion)
    return suggestions


def claude_haiku_correction_provider(
    raw_text: str,
    corrected_text: str,
    confidence: float,
    source_segment_id: str | None,
) -> list[CorrectionSuggestion]:
    if not env_flag("SRT_LLM_RECONCILIATION", False):
        return []
    if correction_key(raw_text) == correction_key(corrected_text):
        return []

    try:
        import boto3  # type: ignore[import-untyped]
    except Exception as exc:
        logger.warning("Claude Haiku correction reconciliation skipped; boto3 unavailable: %s", exc)
        return []

    region = os.environ.get("BEDROCK_REGION", "us-east-1")
    model_id = os.environ.get("CLAUDE_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    client = boto3.client("bedrock-runtime", region_name=region)
    prompt = {
        "raw_asr_text": raw_text,
        "corrected_uploaded_vtt_text": corrected_text,
        "instruction": (
            "Extract only reusable transcription correction patterns. "
            "Return JSON with corrections: [{wrong_text, corrected_text, confidence}]. "
            "Do not rewrite whole sentences. Do not include timing. "
            "Prefer names, technical terms, product names, casing, punctuation, and recurring mistranscriptions."
        ),
    }
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 400,
        "temperature": 0.0,
        "system": "You produce strict JSON for subtitle correction memory.",
        "messages": [{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
    }

    try:
        response = client.invoke_model(
            modelId=model_id,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(body),
        )
        raw_body = response["body"].read().decode("utf-8")
        response_body = json.loads(raw_body)
        text = response_body.get("content", [{}])[0].get("text", "")
        parsed = parse_json_object_from_text(str(text))
        return parse_llm_correction_payload(
            parsed,
            confidence=confidence,
            source_segment_id=source_segment_id,
        )
    except Exception as exc:
        logger.warning("Claude Haiku correction reconciliation failed: %s", exc)
        return []


def dedupe_correction_suggestions(suggestions: list[CorrectionSuggestion]) -> list[CorrectionSuggestion]:
    best_by_key: dict[tuple[str, str], CorrectionSuggestion] = {}
    for suggestion in suggestions:
        key = (correction_key(suggestion.wrong_text), correction_key(suggestion.corrected_text))
        if not key[0] or not key[1] or key[0] == key[1]:
            continue
        existing = best_by_key.get(key)
        if existing is None or suggestion.confidence > existing.confidence:
            best_by_key[key] = suggestion
    return sorted(
        best_by_key.values(),
        key=lambda item: (len(correction_key(item.wrong_text).split()), item.confidence),
        reverse=True,
    )


def correction_pattern(wrong_text: str) -> re.Pattern[str]:
    words = subtitle_word_tokens(wrong_text)
    if words:
        return re.compile(r"(?<!\w)" + r"\s+".join(re.escape(word) for word in words) + r"(?!\w)", re.IGNORECASE)
    return re.compile(re.escape(wrong_text), re.IGNORECASE)


def apply_correction_suggestions_to_text(
    text: str,
    corrections: list[CorrectionSuggestion],
) -> tuple[str, list[CorrectionSuggestion], list[CorrectionSuggestion]]:
    updated_text = text
    applied: list[CorrectionSuggestion] = []
    suggested: list[CorrectionSuggestion] = []

    for correction in corrections:
        pattern = correction_pattern(correction.wrong_text)
        if not pattern.search(updated_text):
            continue
        if correction.confidence >= 0.72:
            updated_text = pattern.sub(correction.corrected_text, updated_text)
            applied.append(
                correction.model_copy(
                    update={
                        "status": "applied",
                        "note": f"Auto-applied learned correction: {correction.wrong_text} -> {correction.corrected_text}.",
                    }
                )
            )
        else:
            suggested.append(
                correction.model_copy(
                    update={
                        "status": "suggested",
                        "note": f"Likely repeated VTT error: {correction.wrong_text} may need {correction.corrected_text}.",
                    }
                )
            )

    return updated_text, applied, suggested


def align_subtitle_segments_by_text(
    *,
    old_segments: list[TranscriptSegment],
    new_timing_segments: list[TranscriptSegment],
) -> list[tuple[int | None, int | None, float]]:
    """Sequence-align old edited subtitles to newly generated timing segments."""
    old_count = len(old_segments)
    new_count = len(new_timing_segments)
    gap_penalty = -0.24
    scores = [[0.0 for _ in range(new_count + 1)] for _ in range(old_count + 1)]
    actions: list[list[Literal["match", "old-gap", "new-gap"] | None]] = [
        [None for _ in range(new_count + 1)] for _ in range(old_count + 1)
    ]

    for old_index in range(1, old_count + 1):
        scores[old_index][0] = scores[old_index - 1][0] + gap_penalty
        actions[old_index][0] = "old-gap"
    for new_index in range(1, new_count + 1):
        scores[0][new_index] = scores[0][new_index - 1] + gap_penalty
        actions[0][new_index] = "new-gap"

    for old_index in range(1, old_count + 1):
        for new_index in range(1, new_count + 1):
            match_score = score_subtitle_text_match(
                old_segments[old_index - 1].text,
                new_timing_segments[new_index - 1].text,
            )
            candidates: list[tuple[float, Literal["match", "old-gap", "new-gap"]]] = [
                (scores[old_index - 1][new_index - 1] + match_score, "match"),
                (scores[old_index - 1][new_index] + gap_penalty, "old-gap"),
                (scores[old_index][new_index - 1] + gap_penalty, "new-gap"),
            ]
            best_score, best_action = max(candidates, key=lambda candidate: candidate[0])
            scores[old_index][new_index] = best_score
            actions[old_index][new_index] = best_action

    aligned: list[tuple[int | None, int | None, float]] = []
    old_index = old_count
    new_index = new_count
    while old_index > 0 or new_index > 0:
        action = actions[old_index][new_index]
        if action == "match" and old_index > 0 and new_index > 0:
            match_score = score_subtitle_text_match(
                old_segments[old_index - 1].text,
                new_timing_segments[new_index - 1].text,
            )
            aligned.append((old_index - 1, new_index - 1, match_score))
            old_index -= 1
            new_index -= 1
        elif action == "old-gap" and old_index > 0:
            aligned.append((old_index - 1, None, 0.0))
            old_index -= 1
        else:
            aligned.append((None, new_index - 1, 0.0))
            new_index -= 1

    aligned.reverse()
    return aligned


def extend_segment_end_from_compatible_legacy_timing(
    *,
    segment: TranscriptSegment,
    old_segment: TranscriptSegment,
    next_timing_segment: TranscriptSegment | None,
    group_start_seconds: float,
    threshold: float,
    confidence: float,
    allow_prefix_clip_extension: bool = False,
) -> tuple[TranscriptSegment, str | None]:
    """Keep trusted legacy cue endings when WhisperX ends a matched cue too early."""
    if confidence < max(threshold, 0.9) and not allow_prefix_clip_extension:
        return segment, None

    legacy_end = old_segment.end_seconds
    if legacy_end <= segment.end_seconds + 0.25:
        return segment, None

    start_tolerance = 4.0 if allow_prefix_clip_extension else 1.75
    if abs(old_segment.start_seconds - group_start_seconds) > start_tolerance:
        return segment, None

    max_allowed_end = legacy_end
    if next_timing_segment is not None and next_timing_segment.start_seconds > segment.start_seconds:
        max_allowed_end = min(max_allowed_end, next_timing_segment.start_seconds - 0.02)

    adjusted_end = round(max(segment.end_seconds, max_allowed_end), 3)
    if adjusted_end <= segment.end_seconds + 0.25:
        return segment, None

    return (
        segment.model_copy(update={"end_seconds": adjusted_end}),
        "End timing was extended from the uploaded VTT because the current alignment ended this matched cue early.",
    )


def find_merged_old_cue_overrides(
    *,
    alignments: list[tuple[int | None, int | None, float]],
    old_segments: list[TranscriptSegment],
    new_timing_segments: list[TranscriptSegment],
    threshold: float,
) -> dict[int, tuple[list[int], str, float]]:
    """Detect when one current timing segment covers multiple previous VTT cues."""
    overrides: dict[int, tuple[list[int], str, float]] = {}
    minimum_merge_confidence = max(threshold, 0.78)

    for alignment_index, (old_index, new_index, confidence) in enumerate(alignments):
        if old_index is None or new_index is None or confidence < threshold:
            continue

        previous_old_indexes: list[int] = []
        scan_index = alignment_index - 1
        while scan_index >= 0:
            candidate_old_index, candidate_new_index, _ = alignments[scan_index]
            if candidate_old_index is None or candidate_new_index is not None:
                break
            previous_old_indexes.append(candidate_old_index)
            scan_index -= 1
        previous_old_indexes.reverse()

        next_old_indexes: list[int] = []
        scan_index = alignment_index + 1
        while scan_index < len(alignments):
            candidate_old_index, candidate_new_index, _ = alignments[scan_index]
            if candidate_old_index is None or candidate_new_index is not None:
                break
            next_old_indexes.append(candidate_old_index)
            scan_index += 1

        best_old_indexes: list[int] = []
        best_text = ""
        best_confidence = confidence
        for previous_count in range(len(previous_old_indexes) + 1):
            for next_count in range(len(next_old_indexes) + 1):
                candidate_old_indexes = [
                    *previous_old_indexes[len(previous_old_indexes) - previous_count :],
                    old_index,
                    *next_old_indexes[:next_count],
                ]
                if len(candidate_old_indexes) < 2:
                    continue
                if candidate_old_indexes != list(
                    range(candidate_old_indexes[0], candidate_old_indexes[-1] + 1)
                ):
                    continue
                candidate_text = "\n".join(
                    old_segments[candidate_old_index].text
                    for candidate_old_index in candidate_old_indexes
                )
                candidate_confidence = score_subtitle_text_match(
                    candidate_text,
                    new_timing_segments[new_index].text,
                )
                if candidate_confidence <= confidence + 0.08:
                    continue
                if candidate_confidence < minimum_merge_confidence:
                    continue
                if candidate_confidence > best_confidence:
                    best_old_indexes = candidate_old_indexes
                    best_text = candidate_text
                    best_confidence = candidate_confidence

        if best_old_indexes:
            overrides[new_index] = (best_old_indexes, best_text, best_confidence)

    return overrides


def retime_edited_subtitle_segments(
    *,
    old_segments: list[TranscriptSegment],
    new_timing_segments: list[TranscriptSegment],
    source_file_name: str,
    source_format: Literal["srt", "vtt"],
    threshold: float,
    llm_correction_provider: LlmCorrectionProvider | None = None,
) -> tuple[list[TranscriptSegment], RetimeEditedSubtitlesReport]:
    if not new_timing_segments:
        raise HTTPException(status_code=400, detail="Current job has no timing segments to retime against.")

    matched_old_indexes: set[int] = set()
    segment_reports: list[RetimedSegmentReport] = []
    retimed_segments: list[TranscriptSegment] = []
    confidence_values: list[float] = []
    alignments = align_subtitle_segments_by_text(
        old_segments=old_segments,
        new_timing_segments=new_timing_segments,
    )
    split_text_by_new_index: dict[int, tuple[int, str, float]] = {}
    split_group_start_by_new_index: dict[int, float] = {}
    split_group_last_new_indexes: set[int] = set()
    skipped_split_new_indexes: set[int] = set()
    for alignment_index, (old_index, new_index, confidence) in enumerate(alignments):
        if old_index is None or new_index is None or confidence < threshold:
            continue

        previous_new_gap_indexes: list[int] = []
        scan_index = alignment_index - 1
        while scan_index >= 0:
            previous_old_index, previous_new_index, _ = alignments[scan_index]
            if previous_old_index is not None or previous_new_index is None:
                break
            previous_new_gap_indexes.append(previous_new_index)
            scan_index -= 1
        previous_new_gap_indexes.reverse()

        next_new_gap_indexes: list[int] = []
        scan_index = alignment_index + 1
        while scan_index < len(alignments):
            next_old_index, next_new_index, _ = alignments[scan_index]
            if next_old_index is not None or next_new_index is None:
                break
            next_new_gap_indexes.append(next_new_index)
            scan_index += 1

        best_split: list[str] | None = None
        best_new_indexes: list[int] = [new_index]
        best_split_confidence = confidence
        for previous_count in range(len(previous_new_gap_indexes) + 1):
            for next_count in range(len(next_new_gap_indexes) + 1):
                candidate_new_indexes = [
                    *previous_new_gap_indexes[len(previous_new_gap_indexes) - previous_count :],
                    new_index,
                    *next_new_gap_indexes[:next_count],
                ]
                if len(candidate_new_indexes) < 2:
                    continue
                candidate_texts = [
                    new_timing_segments[candidate_new_index].text
                    for candidate_new_index in candidate_new_indexes
                ]
                split_chunks = split_corrected_text_across_timing_segments(
                    corrected_text=old_segments[old_index].text,
                    current_texts=candidate_texts,
                )
                if not split_chunks:
                    continue
                split_confidence = max(
                    confidence,
                    score_subtitle_text_match(
                        old_segments[old_index].text,
                        " ".join(candidate_texts),
                    ),
                )
                if split_confidence > best_split_confidence or (
                    split_confidence == best_split_confidence
                    and len(candidate_new_indexes) > len(best_new_indexes)
                ):
                    best_split = split_chunks
                    best_new_indexes = list(candidate_new_indexes)
                    best_split_confidence = split_confidence

        if best_split and len(best_split) == len(best_new_indexes):
            best_split, skipped_indexes = collapse_short_trailing_split_chunks(
                corrected_text=old_segments[old_index].text,
                chunks=best_split,
                timing_segments=[
                    new_timing_segments[best_new_index]
                    for best_new_index in best_new_indexes
                ],
            )
            best_skipped_new_indexes = {
                best_new_indexes[skipped_index]
                for skipped_index in skipped_indexes
            }
            effective_new_indexes = [
                best_new_index
                for index, best_new_index in enumerate(best_new_indexes)
                if index not in set(skipped_indexes)
            ]
            if not effective_new_indexes:
                continue

            split_group_start = new_timing_segments[best_new_indexes[0]].start_seconds
            split_group_last_new_indexes.add(effective_new_indexes[-1])
            skipped_split_new_indexes.update(best_skipped_new_indexes)
            for covered_new_index, chunk_text in zip(best_new_indexes, best_split):
                if covered_new_index in best_skipped_new_indexes:
                    continue
                existing_split = split_text_by_new_index.get(covered_new_index)
                if existing_split is not None and existing_split[2] > best_split_confidence:
                    continue
                split_text_by_new_index[covered_new_index] = (
                    old_index,
                    chunk_text,
                    best_split_confidence,
                )
                split_group_start_by_new_index[covered_new_index] = split_group_start
    merged_old_cues_by_new_index = find_merged_old_cue_overrides(
        alignments=alignments,
        old_segments=old_segments,
        new_timing_segments=new_timing_segments,
        threshold=threshold,
    )

    correction_provider = llm_correction_provider or claude_haiku_correction_provider
    learned_corrections = dedupe_correction_suggestions(
        [
            suggestion
            for old_index, new_index, confidence in alignments
            if (
                old_index is not None
                and new_index is not None
                and confidence >= threshold
            )
            for suggestion in extract_correction_suggestions_from_pair(
                raw_text=new_timing_segments[new_index].text,
                corrected_text=old_segments[old_index].text,
                confidence=confidence,
                source_segment_id=new_timing_segments[new_index].segment_id,
            )
            + correction_provider(
                new_timing_segments[new_index].text,
                old_segments[old_index].text,
                confidence,
                new_timing_segments[new_index].segment_id,
            )
        ]
    )
    applied_correction_count = 0

    for old_index, new_index, confidence in alignments:
        if new_index is None:
            continue
        if new_index in skipped_split_new_indexes:
            continue

        new_segment = new_timing_segments[new_index]
        next_timing_index = new_index + 1
        while next_timing_index in skipped_split_new_indexes:
            next_timing_index += 1
        next_timing_segment = (
            new_timing_segments[next_timing_index]
            if next_timing_index < len(new_timing_segments)
            else None
        )
        split_override = split_text_by_new_index.get(new_index)
        if split_override is not None:
            split_old_index, split_text, split_confidence = split_override
            old_segment = old_segments[split_old_index]
            old_segment_id = old_segment.segment_id
            matched_old_indexes.add(split_old_index)
            status = "matched"
            note = "Previous edited VTT cue was split across current timing segments."
            timed_segment = new_segment
            timing_note = None
            if new_index in split_group_last_new_indexes:
                timed_segment, timing_note = extend_segment_end_from_compatible_legacy_timing(
                    segment=new_segment,
                    old_segment=old_segment,
                    next_timing_segment=next_timing_segment,
                    group_start_seconds=split_group_start_by_new_index.get(
                        new_index,
                        new_segment.start_seconds,
                    ),
                    threshold=threshold,
                    confidence=split_confidence,
                )
                if timing_note:
                    note = f"{note} {timing_note}"
            next_segment = timed_segment.model_copy(
                update={
                    "text": split_text,
                    "speaker": old_segment.speaker,
                    "retime_confidence": round(split_confidence, 3),
                    "retime_status": status,
                    "retime_note": note,
                    "correction_suggestions": [],
                }
            )
            confidence_values.append(split_confidence)
            retimed_segments.append(next_segment)
            segment_reports.append(
                RetimedSegmentReport(
                    segment_id=new_segment.segment_id,
                    old_segment_id=old_segment_id,
                    confidence=round(split_confidence, 3),
                    status=status,
                    note=note,
                    correction_suggestions=[],
                )
            )
            continue

        merge_override = (
            None
            if new_index in split_text_by_new_index
            else merged_old_cues_by_new_index.get(new_index)
        )
        if merge_override is not None:
            merged_old_indexes, merged_text, merge_confidence = merge_override
            old_segment_id = old_segments[merged_old_indexes[0]].segment_id
            matched_old_indexes.update(merged_old_indexes)
            status = "matched"
            note = "Multiple previous VTT cues were merged into this current timing segment."
            next_segment = new_segment.model_copy(
                update={
                    "text": merged_text,
                    "speaker": old_segments[merged_old_indexes[0]].speaker,
                    "retime_confidence": round(merge_confidence, 3),
                    "retime_status": status,
                    "retime_note": note,
                    "correction_suggestions": [],
                }
            )
            confidence_values.append(merge_confidence)
            retimed_segments.append(next_segment)
            segment_reports.append(
                RetimedSegmentReport(
                    segment_id=new_segment.segment_id,
                    old_segment_id=old_segment_id,
                    confidence=round(merge_confidence, 3),
                    status=status,
                    note=note,
                    correction_suggestions=[],
                )
            )
            continue

        old_segment = old_segments[old_index] if old_index is not None else None
        old_segment_id = old_segment.segment_id if old_segment is not None else None
        note: str | None = None
        correction_suggestions: list[CorrectionSuggestion] = []

        if old_segment is None:
            corrected_text, applied, suggested = apply_correction_suggestions_to_text(
                new_segment.text,
                learned_corrections,
            )
            correction_suggestions = [*applied, *suggested]
            applied_correction_count += len(applied)
            if applied:
                status: RetimeStatus = "corrected"
                note = "New video material kept on current timing; learned corrections were auto-applied."
            elif suggested:
                status = "sore-thumb"
                note = "New video material kept on current timing; possible repeated VTT errors need review."
            else:
                status = "new-only"
                note = "No matching previous edit was found for this new timing segment."
            next_segment = new_segment.model_copy(
                update={
                    "text": corrected_text,
                    "retime_confidence": 0.0,
                    "retime_status": status,
                    "retime_note": note,
                    "correction_suggestions": correction_suggestions,
                }
            )
        elif confidence >= threshold:
            matched_old_indexes.add(old_index)
            status = "matched"
            timed_segment, timing_note = extend_segment_end_from_compatible_legacy_timing(
                segment=new_segment,
                old_segment=old_segment,
                next_timing_segment=next_timing_segment,
                group_start_seconds=new_segment.start_seconds,
                threshold=threshold,
                confidence=confidence,
            )
            next_segment = timed_segment.model_copy(
                update={
                    "text": old_segment.text,
                    "speaker": old_segment.speaker,
                    "retime_confidence": round(confidence, 3),
                    "retime_status": status,
                    "retime_note": timing_note,
                    "correction_suggestions": [],
                }
            )
            confidence_values.append(confidence)
        elif (preserve_note := is_low_confidence_clip_preservable(
            old_segment=old_segment,
            new_segment=new_segment,
            confidence=confidence,
            threshold=threshold,
        ))[0]:
            preserve_explanation = preserve_note[1] or "Preserved the previously edited VTT text on a low-confidence match."
            matched_old_indexes.add(old_index)
            status = "matched"
            prefix_confidence = max(confidence, threshold)
            timed_segment, timing_note = extend_segment_end_from_compatible_legacy_timing(
                segment=new_segment,
                old_segment=old_segment,
                next_timing_segment=next_timing_segment,
                group_start_seconds=new_segment.start_seconds,
                threshold=threshold,
                confidence=prefix_confidence,
                allow_prefix_clip_extension=True,
            )
            note = preserve_explanation
            if timing_note:
                note = f"{note} {timing_note}"
            next_segment = timed_segment.model_copy(
                update={
                    "text": old_segment.text,
                    "speaker": old_segment.speaker,
                    "retime_confidence": round(prefix_confidence, 3),
                    "retime_status": status,
                    "retime_note": note,
                    "correction_suggestions": [],
                }
            )
            confidence_values.append(prefix_confidence)
        elif is_prefix_clipped_match(old_segment.text, new_segment.text):
            matched_old_indexes.add(old_index)
            status = "matched"
            prefix_confidence = max(confidence, threshold)
            timed_segment, timing_note = extend_segment_end_from_compatible_legacy_timing(
                segment=new_segment,
                old_segment=old_segment,
                next_timing_segment=next_timing_segment,
                group_start_seconds=new_segment.start_seconds,
                threshold=threshold,
                confidence=prefix_confidence,
                allow_prefix_clip_extension=True,
            )
            note = "Current timing text only contained the start of the uploaded VTT cue; preserved the VTT tail."
            if timing_note:
                note = f"{note} {timing_note}"
            next_segment = timed_segment.model_copy(
                update={
                    "text": old_segment.text,
                    "speaker": old_segment.speaker,
                    "retime_confidence": round(prefix_confidence, 3),
                    "retime_status": status,
                    "retime_note": note,
                    "correction_suggestions": [],
                }
            )
            confidence_values.append(prefix_confidence)
        else:
            corrected_text, applied, suggested = apply_correction_suggestions_to_text(
                new_segment.text,
                learned_corrections,
            )
            correction_suggestions = [*applied, *suggested]
            applied_correction_count += len(applied)
            if applied:
                status = "corrected"
                note = "Low-confidence old segment was not copied wholesale; learned corrections were auto-applied to current text."
            elif suggested:
                status = "sore-thumb"
                note = "Previous edit was below the retime threshold and likely repeated VTT errors need review."
            else:
                status = "low-confidence"
                note = "Previous edit was similar but below the retime confidence threshold."
            next_segment = new_segment.model_copy(
                update={
                    "text": corrected_text,
                    "retime_confidence": round(confidence, 3),
                    "retime_status": status,
                    "retime_note": note,
                    "correction_suggestions": correction_suggestions,
                }
            )
            confidence_values.append(confidence)

        retimed_segments.append(next_segment)
        segment_reports.append(
            RetimedSegmentReport(
                segment_id=new_segment.segment_id,
                old_segment_id=old_segment_id,
                confidence=round(confidence, 3),
                status=status,
                note=note,
                correction_suggestions=correction_suggestions,
            )
        )

    unmatched_old_count = len(old_segments) - len(matched_old_indexes)
    sore_thumb_count = sum(1 for report in segment_reports if report.status == "sore-thumb")
    low_confidence_count = sum(
        1 for report in segment_reports if report.status in {"low-confidence", "sore-thumb"}
    )
    new_only_count = sum(
        1 for report in segment_reports if report.status in {"new-only", "corrected", "sore-thumb"}
    )
    matched_count = sum(1 for report in segment_reports if report.status == "matched")
    average_confidence = (
        round(sum(confidence_values) / len(confidence_values), 3)
        if confidence_values
        else 0.0
    )

    report = RetimeEditedSubtitlesReport(
        source_file_name=source_file_name,
        source_format=source_format,
        matched_segments=matched_count,
        low_confidence_segments=low_confidence_count,
        unmatched_old_segments=max(unmatched_old_count, 0),
        unmatched_new_segments=new_only_count,
        average_confidence=average_confidence,
        threshold=round(threshold, 3),
        created_at=datetime.now(timezone.utc).isoformat(),
        learned_corrections=learned_corrections,
        applied_corrections=applied_correction_count,
        sore_thumb_segments=sore_thumb_count,
        segments=segment_reports,
    )
    return retimed_segments, report


def apply_legacy_subtitle_to_job(
    *,
    job: JobDetail,
    subtitle_path: Path,
    source_file_name: str,
    threshold: float = 0.58,
    snapshot_label_prefix: str = "Before legacy VTT apply",
    origin_note: str = "Legacy subtitle file applied after current timing generation",
) -> tuple[JobDetail, RetimeEditedSubtitlesReport]:
    content, format_name = load_subtitle_text_from_path(subtitle_path)
    old_segments = parse_subtitle_text_to_segments(
        content=content,
        format_name=format_name,
        job_id=f"{job.job_id}-legacy",
    )
    base_job = ensure_default_edited_track(job)
    retimed_segments, report = retime_edited_subtitle_segments(
        old_segments=old_segments,
        new_timing_segments=base_job.transcript_segments,
        source_file_name=source_file_name,
        source_format=format_name,
        threshold=threshold,
    )
    track_count = len(base_job.subtitle_tracks) + 1
    snapshot_path = DATA_DIR / "tracks" / f"{job.job_id}-pre-retime-{track_count:03d}.srt"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(render_srt(base_job.transcript_segments), encoding="utf-8")
    snapshot_track = create_stored_track(
        job=base_job,
        source_kind="pre-retime-snapshot",
        format_name="srt",
        subtitle_path=str(snapshot_path),
        label=f"{snapshot_label_prefix} {track_count:03d}",
        language="eng",
        origin_note="Recoverable transcript snapshot before applying legacy subtitle text",
        is_default=False,
    )
    source_track = create_stored_track(
        job=base_job.model_copy(update={"subtitle_tracks": [*base_job.subtitle_tracks, snapshot_track]}),
        source_kind="retimed-edits",
        format_name=format_name,
        subtitle_path=str(subtitle_path),
        label=Path(source_file_name).stem,
        language="eng",
        origin_note=f"{origin_note}; average confidence={report.average_confidence}",
        is_default=False,
    )
    updated_job = renumber_tracks(
        base_job.model_copy(
            update={
                "transcript_segments": retimed_segments,
                "transcript_is_edited": True,
                "transcription_mode": "real-cli",
                "transcription_source": f"retimed-edits:{format_name}",
                "timing_source": f"{base_job.timing_source}|retimed-edits:{format_name}",
                "subtitle_tracks": [*base_job.subtitle_tracks, snapshot_track, source_track],
                "pending_legacy_subtitle_path": None,
                "pending_legacy_subtitle_name": None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    )
    return updated_job, report


def apply_pending_legacy_subtitle(job_id: str) -> None:
    with jobs_lock:
        target_index = next((index for index, job in enumerate(jobs) if job.job_id == job_id), None)
        if target_index is None:
            return
        job = jobs[target_index]
        pending_path_raw = job.pending_legacy_subtitle_path
        pending_name = job.pending_legacy_subtitle_name
    if not pending_path_raw:
        return

    subtitle_path = Path(pending_path_raw).expanduser()
    try:
        updated_job, report = apply_legacy_subtitle_to_job(
            job=job,
            subtitle_path=subtitle_path,
            source_file_name=pending_name or subtitle_path.name,
            threshold=0.58,
            snapshot_label_prefix="Before startup legacy apply",
            origin_note="Legacy subtitle uploaded with source video and auto-applied after timing generation",
        )
        logger.info(
            "Auto-applied legacy subtitle for %s: matched=%s low_confidence=%s",
            job_id,
            report.matched_segments,
            report.low_confidence_segments,
        )
    except Exception as exc:
        logger.exception("Failed to auto-apply legacy subtitle for %s (%s)", job_id, exc)
        with jobs_lock:
            for index, existing_job in enumerate(jobs):
                if existing_job.job_id != job_id:
                    continue
                jobs[index] = existing_job.model_copy(
                    update={
                        "transcription_source": f"{existing_job.transcription_source}|legacy-vtt-auto-apply-failed:{type(exc).__name__}",
                        "pending_legacy_subtitle_path": None,
                        "pending_legacy_subtitle_name": None,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                break
        save_state()
        return

    with jobs_lock:
        for index, existing_job in enumerate(jobs):
            if existing_job.job_id == job_id:
                jobs[index] = updated_job
                break
    save_state()


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


def find_whisperx_command() -> tuple[list[str] | None, str]:
    whisperx_binary = shutil.which("whisperx")
    if whisperx_binary:
        return [whisperx_binary], "whisperx-ready:cli"

    virtualenv = os.getenv("VIRTUAL_ENV")
    if virtualenv:
        virtualenv_whisperx = Path(virtualenv) / "bin" / "whisperx"
        if virtualenv_whisperx.exists():
            return [str(virtualenv_whisperx)], "whisperx-ready:venv-cli"

    project_venv_whisperx = DATA_DIR.parent.parent / ".venv" / "bin" / "whisperx"
    if project_venv_whisperx.exists():
        return [str(project_venv_whisperx)], "whisperx-ready:project-venv-cli"

    try:
        if importlib.util.find_spec("whisperx") is not None:
            return [sys.executable, "-m", "whisperx"], "whisperx-ready:python-module"
    except (ImportError, ValueError):
        return None, "whisperx-unavailable:module-check-error"

    return None, "whisperx-unavailable:not-installed"


def detect_whisperx_availability() -> tuple[bool, str]:
    whisperx_command, whisperx_status = find_whisperx_command()
    return whisperx_command is not None, whisperx_status


def find_timed_subtitle_output(
    *,
    temp_dir: Path,
    audio_path: Path,
) -> tuple[Path, Literal["srt", "vtt"]] | None:
    preferred_candidates: list[tuple[Path, Literal["srt", "vtt"]]] = [
        (temp_dir / f"{audio_path.stem}.srt", "srt"),
        (temp_dir / f"{audio_path.name}.srt", "srt"),
        (temp_dir / f"{audio_path.stem}.vtt", "vtt"),
        (temp_dir / f"{audio_path.name}.vtt", "vtt"),
    ]
    for candidate_path, format_name in preferred_candidates:
        if candidate_path.exists():
            return candidate_path, format_name

    fallback_candidates: list[tuple[Path, Literal["srt", "vtt"]]] = []
    for path in sorted(temp_dir.glob("*.srt")):
        fallback_candidates.append((path, "srt"))
    for path in sorted(temp_dir.glob("*.vtt")):
        fallback_candidates.append((path, "vtt"))
    return fallback_candidates[0] if fallback_candidates else None


def run_whisperx_transcription(
    *,
    whisperx_command_prefix: list[str],
    audio_path: Path,
    temp_dir: Path,
    job_id: str,
) -> tuple[list[TranscriptSegment] | None, str | None]:
    whisperx_model = os.getenv("LOCAL_WHISPERX_MODEL", os.getenv("LOCAL_WHISPER_MODEL", "tiny"))
    whisperx_device = os.getenv("LOCAL_WHISPERX_DEVICE", "cpu")
    whisperx_compute_type = os.getenv("LOCAL_WHISPERX_COMPUTE_TYPE", "int8")
    whisperx_timeout_raw = os.getenv("LOCAL_WHISPERX_TIMEOUT_SECONDS", "1800")
    try:
        whisperx_timeout_seconds = max(60, int(whisperx_timeout_raw))
    except ValueError:
        whisperx_timeout_seconds = 1800

    cli_command = [
        *whisperx_command_prefix,
        str(audio_path),
        "--model",
        whisperx_model,
        "--output_format",
        "srt",
        "--output_dir",
        str(temp_dir),
        "--device",
        whisperx_device,
        "--compute_type",
        whisperx_compute_type,
    ]
    whisper_language = os.getenv("LOCAL_WHISPER_LANGUAGE")
    if whisper_language:
        cli_command.extend(["--language", whisper_language])

    try:
        cli_result = subprocess.run(
            cli_command,
            check=False,
            capture_output=True,
            text=True,
            timeout=whisperx_timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return None, "whisperx-run-timeout"
    except OSError:
        return None, "whisperx-run-failed"

    if cli_result.returncode != 0:
        return None, "whisperx-error"

    subtitle_output = find_timed_subtitle_output(temp_dir=temp_dir, audio_path=audio_path)
    if subtitle_output is None:
        return None, "whisperx-no-output"
    subtitle_path, format_name = subtitle_output

    try:
        subtitle_content = subtitle_path.read_text(encoding="utf-8-sig")
    except OSError:
        return None, "whisperx-read-failed"
    try:
        timed_segments = parse_subtitle_text_to_segments(
            content=subtitle_content,
            format_name=format_name,
            job_id=job_id,
        )
    except (HTTPException, ValueError):
        return None, "whisperx-parse-failed"

    if not timed_segments:
        return None, "whisperx-empty"
    return timed_segments, None


def try_local_cli_transcription(
    *,
    media_path: Path,
    job_id: str,
    media_metadata: MediaMetadata | None,
) -> tuple[list[TranscriptSegment] | None, str, str, str]:
    whisperx_command_prefix, whisperx_status = find_whisperx_command()
    whisperx_available = whisperx_command_prefix is not None
    ffmpeg_binary = shutil.which("ffmpeg")
    if ffmpeg_binary is None:
        return (
            None,
            "placeholder",
            "placeholder-fallback:ffmpeg-missing",
            f"placeholder-fallback:ffmpeg-missing|{whisperx_status}",
        )

    whisper_cli_name, whisper_cli_command_prefix = find_transcription_cli()
    if whisper_cli_name is None and whisperx_command_prefix is None:
        return (
            None,
            "placeholder",
            "placeholder-fallback:transcriber-missing",
            f"placeholder-fallback:transcriber-missing|{whisperx_status}",
        )

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
            return (
                None,
                "placeholder",
                "placeholder-fallback:ffmpeg-extract-failed",
                f"placeholder-fallback:ffmpeg-extract-failed|{whisperx_status}",
            )
        if extract_result.returncode != 0 or not audio_path.exists():
            return (
                None,
                "placeholder",
                "placeholder-fallback:ffmpeg-extract-failed",
                f"placeholder-fallback:ffmpeg-extract-failed|{whisperx_status}",
            )

        whisperx_failure_reason: str | None = None
        if whisperx_command_prefix:
            whisperx_segments, whisperx_error = run_whisperx_transcription(
                whisperx_command_prefix=whisperx_command_prefix,
                audio_path=audio_path,
                temp_dir=temp_dir,
                job_id=job_id,
            )
            if whisperx_segments:
                return (
                    whisperx_segments,
                    "real-cli",
                    "real-cli:whisperx",
                    "real-cli:whisperx-aligned",
                )
            whisperx_failure_reason = whisperx_error or "whisperx-error"

        if whisper_cli_name == "whisper" and whisper_cli_command_prefix is not None:
            cli_command = [
                *whisper_cli_command_prefix,
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
                return (
                    None,
                    "placeholder",
                    "placeholder-fallback:transcriber-run-failed",
                    f"placeholder-fallback:transcriber-run-failed|{whisperx_status}",
                )
            if cli_result.returncode != 0:
                return (
                    None,
                    "placeholder",
                    "placeholder-fallback:transcriber-error",
                    f"placeholder-fallback:transcriber-error|{whisperx_status}",
                )

            transcript_path = temp_dir / f"{audio_path.stem}.txt"
            if not transcript_path.exists():
                return (
                    None,
                    "placeholder",
                    "placeholder-fallback:transcriber-no-output",
                    f"placeholder-fallback:transcriber-no-output|{whisperx_status}",
                )

            transcript_text = transcript_path.read_text(encoding="utf-8").strip()
            real_segments = split_text_into_segments(
                text=transcript_text,
                job_id=job_id,
                total_duration_seconds=(
                    media_metadata.duration_seconds if media_metadata else None
                ),
            )
            if not real_segments:
                return (
                    None,
                    "placeholder",
                    "placeholder-fallback:no-speech-detected",
                    f"placeholder-fallback:no-speech-detected|{whisperx_status}",
                )
            timing_source = (
                f"whisperx-fallback:whisper-cli-estimated|{whisperx_failure_reason}"
                if whisperx_failure_reason
                else "plain-whisper-cli-estimated"
            )
            return real_segments, "real-cli", "real-cli:whisper", timing_source

    return (
        None,
        "placeholder",
        "placeholder-fallback:transcriber-unusable",
        (
            f"placeholder-fallback:transcriber-unusable|{whisperx_status}|{whisperx_failure_reason}"
            if whisperx_failure_reason
            else f"placeholder-fallback:transcriber-unusable|{whisperx_status}"
        ),
    )


def update_job_processing_state(
    job_id: str,
    *,
    stage: JobStage | None = None,
    transcription_mode: Literal["placeholder", "real-cli"] | None = None,
    transcription_source: str | None = None,
    timing_source: str | None = None,
    transcript_segments: list[TranscriptSegment] | None = None,
) -> None:
    with jobs_lock:
        for index, job in enumerate(jobs):
            if job.job_id != job_id:
                continue
            next_stage = stage or job.stage
            preserve_edited_transcript = job.transcript_is_edited and transcript_segments is not None
            next_job = job.model_copy(
                update={
                    "stage": next_stage,
                    "progress_percent": STAGE_PROGRESS[next_stage],
                    "stage_label": STAGE_LABELS[next_stage],
                    "stage_description": STAGE_DESCRIPTIONS[next_stage],
                    "transcription_mode": (
                        job.transcription_mode
                        if preserve_edited_transcript and transcription_mode is not None
                        else transcription_mode or job.transcription_mode
                    ),
                    "transcription_source": (
                        job.transcription_source
                        if preserve_edited_transcript and transcription_source is not None
                        else transcription_source or job.transcription_source
                    ),
                    "timing_source": (
                        job.timing_source
                        if preserve_edited_transcript and timing_source is not None
                        else timing_source or job.timing_source
                    ),
                    "transcript_segments": (
                        job.transcript_segments
                        if preserve_edited_transcript
                        else transcript_segments or job.transcript_segments
                    ),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            jobs[index] = next_job
            break
    save_state()


def run_ingest_pipeline(job_id: str, media_path: Path) -> None:
    global active_ingest_job_id
    with ingest_worker_lock:
        active_ingest_job_id = job_id
        try:
            run_ingest_pipeline_unlocked(job_id, media_path)
        finally:
            active_ingest_job_id = None


def run_ingest_pipeline_unlocked(job_id: str, media_path: Path) -> None:
    try:
        update_job_processing_state(job_id, stage="probing")
        media_metadata = build_media_metadata(media_path)
        with jobs_lock:
            for index, job in enumerate(jobs):
                if job.job_id != job_id:
                    continue
                jobs[index] = job.model_copy(
                    update={
                        "media_metadata": media_metadata,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                break
        save_state()

        time.sleep(0.15)
        update_job_processing_state(job_id, stage="transcribing")
        local_segments, detected_mode, detected_source, detected_timing_source = try_local_cli_transcription(
            media_path=media_path,
            job_id=job_id,
            media_metadata=media_metadata,
        )

        time.sleep(0.15)
        aligned_segments = local_segments or build_placeholder_segments(
            stage="aligned",
            job_id=job_id,
            media_metadata=media_metadata,
        )
        update_job_processing_state(
            job_id,
            stage="aligned",
            transcription_mode=detected_mode if local_segments else "placeholder",
            transcription_source=detected_source,
            timing_source=detected_timing_source,
            transcript_segments=aligned_segments,
        )

        time.sleep(0.1)
        final_stage: JobStage = "ready"
        final_segments = aligned_segments
        if local_segments:
            update_job_processing_state(
                job_id,
                stage=final_stage,
                transcription_mode=detected_mode,
                transcription_source=detected_source,
                timing_source=detected_timing_source,
                transcript_segments=final_segments,
            )
        else:
            ready_segments = build_placeholder_segments(
                stage="ready",
                job_id=job_id,
                media_metadata=media_metadata,
            )
            update_job_processing_state(
                job_id,
                stage=final_stage,
                transcription_mode="placeholder",
                transcription_source=detected_source,
                timing_source=detected_timing_source,
                transcript_segments=ready_segments,
            )
        apply_pending_legacy_subtitle(job_id)
    except Exception as exc:
        logger.exception("Ingest pipeline failed for %s (%s)", job_id, media_path)
        fallback_metadata = None
        try:
            fallback_metadata = build_media_metadata(media_path)
        except Exception:
            fallback_metadata = None
        update_job_processing_state(
            job_id,
            stage="ready",
            transcription_mode="placeholder",
            transcription_source=f"placeholder-fallback:pipeline-error:{type(exc).__name__}",
            timing_source=f"placeholder-fallback:pipeline-error:{type(exc).__name__}",
            transcript_segments=build_placeholder_segments(
                stage="ready",
                job_id=job_id,
                media_metadata=fallback_metadata,
            ),
        )


def create_ingest_job(
    *,
    media_path: Path,
    pending_legacy_subtitle_path: Path | None = None,
    pending_legacy_subtitle_name: str | None = None,
) -> IngestResponse:
    global next_job_id
    with jobs_lock:
        synthetic_job_id = f"job-placeholder-{next_job_id:03d}"
        next_job_id += 1
        stage: JobStage = "queued"
        now = datetime.now(timezone.utc).isoformat()
        jobs.append(
            JobDetail(
                job_id=synthetic_job_id,
                kind="ingest",
                media_path=str(media_path),
                media_metadata=None,
                transcription_mode="placeholder",
                transcription_source="queued-awaiting-processing",
                timing_source="queued-awaiting-processing",
                stage=stage,
                progress_percent=STAGE_PROGRESS[stage],
                stage_label=STAGE_LABELS[stage],
                stage_description=STAGE_DESCRIPTIONS[stage],
                created_at=now,
                updated_at=now,
                transcript_segments=build_placeholder_segments(
                    stage=stage,
                    job_id=synthetic_job_id,
                    media_metadata=None,
                ),
                pending_legacy_subtitle_path=(
                    str(pending_legacy_subtitle_path) if pending_legacy_subtitle_path else None
                ),
                pending_legacy_subtitle_name=pending_legacy_subtitle_name,
            )
        )
    save_state()

    worker = threading.Thread(
        target=run_ingest_pipeline,
        args=(synthetic_job_id, media_path),
        daemon=True,
        name=f"subtitle-ingest-{synthetic_job_id}",
    )
    worker.start()

    return IngestResponse(
        job_id=synthetic_job_id,
        status="queued",
        message=(
            f"Ingest accepted and queued for processing: {media_path}"
            + ("; legacy subtitle will auto-apply after timing." if pending_legacy_subtitle_path else "")
        ),
    )


def resolve_legacy_subtitle_path(raw_path: str | None) -> tuple[Path | None, str | None]:
    if raw_path is None or not raw_path.strip():
        return None, None
    normalized_path = Path(raw_path.strip()).expanduser()
    if normalized_path.suffix.lower() not in {".srt", ".vtt"}:
        raise HTTPException(status_code=400, detail="Legacy subtitle path must be .srt or .vtt.")
    if not normalized_path.exists():
        raise HTTPException(
            status_code=400,
            detail=f"Legacy subtitle path does not exist: {normalized_path}",
        )
    if not normalized_path.is_file():
        raise HTTPException(
            status_code=400,
            detail=f"Legacy subtitle path is not a file: {normalized_path}",
        )
    return normalized_path, normalized_path.name


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

    legacy_subtitle_path, legacy_subtitle_name = resolve_legacy_subtitle_path(
        payload.legacy_subtitle_path,
    )

    return create_ingest_job(
        media_path=normalized_path,
        pending_legacy_subtitle_path=legacy_subtitle_path,
        pending_legacy_subtitle_name=legacy_subtitle_name,
    )


async def store_upload_file(
    *,
    upload: UploadFile,
    destination: Path,
    max_bytes: int = UPLOAD_MAX_BYTES,
) -> None:
    try:
        bytes_written = 0
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as output_file:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > max_bytes:
                    destination.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"Uploaded file exceeds size limit ({max_bytes} bytes).",
                    )
                output_file.write(chunk)
    finally:
        await upload.close()

    if destination.stat().st_size == 0:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")


@app.post("/ingest/upload", response_model=IngestResponse)
async def ingest_upload(
    file: UploadFile = File(...),
    legacy_subtitle: UploadFile | None = File(default=None),
    legacy_subtitle_path: str | None = Form(default=None),
    original_path: str | None = Form(default=None),
) -> IngestResponse:
    file_name = Path(file.filename or "upload.bin").name
    suffix = Path(file_name).suffix
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

    global next_job_id
    upload_stem = f"upload-{next_job_id:03d}"
    stored_path = UPLOADS_DIR / f"{upload_stem}{suffix}"

    await store_upload_file(upload=file, destination=stored_path)

    pending_legacy_subtitle_path: Path | None = None
    legacy_subtitle_name: str | None = None
    legacy_upload_filename = getattr(legacy_subtitle, "filename", None)
    if legacy_upload_filename:
        legacy_subtitle_name = Path(legacy_upload_filename).name
        legacy_suffix = Path(legacy_subtitle_name).suffix.lower()
        if legacy_suffix not in {".srt", ".vtt"}:
            await legacy_subtitle.close()
            raise HTTPException(status_code=400, detail="Legacy subtitle must be .srt or .vtt.")
        pending_legacy_subtitle_path = DATA_DIR / "tracks" / f"{upload_stem}-legacy{legacy_suffix}"
        await store_upload_file(
            upload=legacy_subtitle,
            destination=pending_legacy_subtitle_path,
            max_bytes=min(UPLOAD_MAX_BYTES, 100 * 1024 * 1024),
        )
    elif legacy_subtitle_path:
        pending_legacy_subtitle_path, legacy_subtitle_name = resolve_legacy_subtitle_path(
            legacy_subtitle_path,
        )

    ingest_response = create_ingest_job(
        media_path=stored_path,
        pending_legacy_subtitle_path=pending_legacy_subtitle_path,
        pending_legacy_subtitle_name=legacy_subtitle_name,
    )
    if original_path:
        ingest_response.message += f" (uploaded from {original_path})"
    return ingest_response


@app.get("/jobs", response_model=list[JobSummary])
def list_jobs() -> list[JobSummary]:
    prune_expired_data(force=True)
    # TODO: Back this with a real job store (database or durable local state).
    # TODO: Return stage-level progress for ffmpeg decode, WhisperX align, and diarization.
    return [summarize_job(job) for job in jobs]


@app.get("/jobs/{job_id}", response_model=JobDetail)
def get_job(job_id: str) -> JobDetail:
    prune_expired_data(force=True)
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
                "timing_source": f"imported-subtitle:{format_name}",
                "subtitle_tracks": [*base_job.subtitle_tracks, new_track],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ))
        jobs[index] = updated_job
        save_state()
        return updated_job

    raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")


@app.post("/jobs/{job_id}/retime-edited-subtitles", response_model=RetimeEditedSubtitlesResponse)
async def retime_job_edited_subtitles(
    job_id: str,
    file: UploadFile = File(...),
    confidence_threshold: float = Form(default=0.58),
) -> RetimeEditedSubtitlesResponse:
    file_name = (file.filename or "").lower()
    if file_name.endswith(".srt"):
        format_name: Literal["srt", "vtt"] = "srt"
    elif file_name.endswith(".vtt"):
        format_name = "vtt"
    else:
        await file.close()
        raise HTTPException(status_code=400, detail="Previous edit import supports only .srt or .vtt files.")

    if confidence_threshold < 0 or confidence_threshold > 1:
        await file.close()
        raise HTTPException(status_code=400, detail="confidence_threshold must be between 0 and 1.")

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

    old_segments = parse_subtitle_text_to_segments(
        content=content,
        format_name=format_name,
        job_id=f"{job_id}-previous",
    )

    for index, job in enumerate(jobs):
        if job.job_id != job_id:
            continue
        base_job = ensure_default_edited_track(job)
        retimed_segments, report = retime_edited_subtitle_segments(
            old_segments=old_segments,
            new_timing_segments=base_job.transcript_segments,
            source_file_name=file.filename or f"previous-edits.{format_name}",
            source_format=format_name,
            threshold=confidence_threshold,
        )

        track_count = len(base_job.subtitle_tracks) + 1
        snapshot_path = DATA_DIR / "tracks" / f"{job_id}-pre-retime-{track_count:03d}.srt"
        source_path = DATA_DIR / "tracks" / f"{job_id}-retime-source-{track_count + 1:03d}.{format_name}"
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(render_srt(base_job.transcript_segments), encoding="utf-8")
        source_path.write_text(content, encoding="utf-8")

        snapshot_track = create_stored_track(
            job=base_job,
            source_kind="pre-retime-snapshot",
            format_name="srt",
            subtitle_path=str(snapshot_path),
            label="Before previous edits retime",
            language="eng",
            origin_note="Recoverable snapshot before applying previous subtitle edits",
            is_default=False,
        )
        source_track = create_stored_track(
            job=base_job.model_copy(update={"subtitle_tracks": [*base_job.subtitle_tracks, snapshot_track]}),
            source_kind="retimed-edits",
            format_name=format_name,
            subtitle_path=str(source_path),
            label=Path(file.filename or f"Previous edits {format_name.upper()}").stem,
            language="eng",
            origin_note="Previous edited subtitle file used as text authority for retiming",
            is_default=False,
        )

        updated_job = renumber_tracks(
            base_job.model_copy(
                update={
                    "transcript_segments": retimed_segments,
                    "transcript_is_edited": True,
                    "transcription_mode": "real-cli",
                    "transcription_source": f"retimed-edits:{format_name}",
                    "timing_source": f"{base_job.timing_source}|retimed-edits:{format_name}",
                    "subtitle_tracks": [*base_job.subtitle_tracks, snapshot_track, source_track],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        )
        jobs[index] = updated_job
        save_state()
        return RetimeEditedSubtitlesResponse(**updated_job.model_dump(), retime_report=report)

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


@app.get("/jobs/{job_id}/artifacts", response_model=list[GeneratedArtifact])
def list_job_artifacts(job_id: str) -> list[GeneratedArtifact]:
    job = find_job_or_404(job_id)
    normalized_artifacts: list[GeneratedArtifact] = []
    changed = False
    for artifact in job.artifacts:
        artifact_path_value = artifact.artifact_path
        if Path(artifact_path_value).is_absolute():
            try:
                artifact_path_value = to_artifact_relative_path(Path(artifact_path_value))
                changed = True
            except ValueError:
                artifact_path_value = artifact_path_value
        download_url_value = artifact.download_url or build_artifact_download_url(
            job.job_id,
            artifact.artifact_id,
        )
        if artifact.download_url != download_url_value:
            changed = True
        normalized_artifacts.append(
            artifact.model_copy(
                update={
                    "artifact_path": artifact_path_value,
                    "download_url": download_url_value,
                }
            )
        )
    if changed:
        for index, existing_job in enumerate(jobs):
            if existing_job.job_id != job.job_id:
                continue
            updated_job = existing_job.model_copy(
                update={
                    "artifacts": normalized_artifacts,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            jobs[index] = updated_job
            save_state()
            job = updated_job
            break
    return list(reversed(normalized_artifacts if changed else job.artifacts))


@app.post(
    "/jobs/{job_id}/artifacts/build/{format_name}",
    response_model=GeneratedArtifact,
)
def build_job_transcript_artifact(
    job_id: str,
    format_name: Literal["srt", "vtt", "scorm12", "scorm2004", "aicc", "xapi", "cmi5"],
    payload: BuildArtifactRequest | None = None,
) -> GeneratedArtifact:
    for index, job in enumerate(jobs):
        if job.job_id != job_id:
            continue
        next_artifact = build_transcript_artifact(job, format_name, payload.output_filename if payload else None) if format_name in {"srt", "vtt"} else build_package_artifact(job, format_name, payload.output_filename if payload else None)
        updated_job = job.model_copy(
            update={
                "artifacts": [*job.artifacts, next_artifact],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        jobs[index] = updated_job
        save_state()
        return next_artifact
    raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")


@app.post(
    "/jobs/{job_id}/artifacts/build/package/{format_name}",
    response_model=GeneratedArtifact,
)
def build_job_package_artifact(
    job_id: str,
    format_name: Literal["scorm12", "scorm2004", "aicc", "xapi", "cmi5"],
    payload: BuildArtifactRequest | None = None,
) -> GeneratedArtifact:
    for index, job in enumerate(jobs):
        if job.job_id != job_id:
            continue
        if format_name not in PACKAGE_FORMATS:
            raise HTTPException(status_code=400, detail=f"Unsupported package format: {format_name}")
        next_artifact = build_package_artifact(job, format_name, payload.output_filename if payload else None)
        updated_job = job.model_copy(
            update={
                "artifacts": [*job.artifacts, next_artifact],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        jobs[index] = updated_job
        save_state()
        return next_artifact
    raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")


@app.get("/jobs/{job_id}/artifacts/{artifact_id}/download")
def download_job_artifact(job_id: str, artifact_id: str) -> FileResponse:
    job = find_job_or_404(job_id)
    artifact = get_job_artifact(job, artifact_id)
    artifact_path = Path(artifact.artifact_path).expanduser()
    if not artifact_path.is_absolute():
        artifact_path = ARTIFACTS_DIR / artifact_path
    if not artifact_path.exists() or not artifact_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Artifact file is missing on disk: {artifact.file_name}",
        )
    if artifact.format_name == "srt":
        media_type = "application/x-subrip"
    elif artifact.format_name == "vtt":
        media_type = "text/vtt; charset=utf-8"
    elif artifact.format_name in PACKAGE_FORMATS:
        media_type = "application/zip"
    else:
        media_type = "video/mp4"
    return FileResponse(
        path=artifact_path,
        media_type=media_type,
        filename=artifact.file_name,
    )


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
        format_name: Literal["srt", "vtt"] = "srt" if chosen_path.suffix.lower() == ".srt" else "vtt"
        retimed_segments, report = retime_edited_subtitle_segments(
            old_segments=parsed_segments,
            new_timing_segments=base_job.transcript_segments,
            source_file_name=chosen_path.name,
            source_format=format_name,
            threshold=0.58,
        )
        track_count = len(base_job.subtitle_tracks) + 1
        snapshot_path = DATA_DIR / "tracks" / f"{job_id}-pre-sidecar-retime-{track_count:03d}.srt"
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(render_srt(base_job.transcript_segments), encoding="utf-8")
        snapshot_track = create_stored_track(
            job=base_job,
            source_kind="pre-retime-snapshot",
            format_name="srt",
            subtitle_path=str(snapshot_path),
            label=f"Before sidecar apply {track_count:03d}",
            language="eng",
            origin_note="Recoverable transcript snapshot before applying sidecar subtitle text",
            is_default=False,
        )
        new_track = create_stored_track(
            job=base_job,
            source_kind="sidecar-subtitle",
            format_name=format_name,
            subtitle_path=str(chosen_path),
            label=chosen_path.stem,
            language="eng",
            origin_note=f"{source_label} applied to current timing; average confidence={report.average_confidence}",
            is_default=False,
        )
        updated_job = renumber_tracks(base_job.model_copy(
            update={
                "transcript_segments": retimed_segments,
                "transcript_is_edited": True,
                "transcription_mode": "real-cli",
                "transcription_source": f"{source_label}-text-applied",
                "timing_source": base_job.timing_source,
                "subtitle_tracks": [*base_job.subtitle_tracks, snapshot_track, new_track],
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
        retimed_segments, report = retime_edited_subtitle_segments(
            old_segments=parsed_segments,
            new_timing_segments=base_job.transcript_segments,
            source_file_name=f"{source_label}.vtt",
            source_format="vtt",
            threshold=0.58,
        )
        track_count = len(base_job.subtitle_tracks) + 1
        snapshot_path = DATA_DIR / "tracks" / f"{job_id}-pre-embedded-retime-{track_count:03d}.srt"
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(render_srt(base_job.transcript_segments), encoding="utf-8")
        snapshot_track = create_stored_track(
            job=base_job,
            source_kind="pre-retime-snapshot",
            format_name="srt",
            subtitle_path=str(snapshot_path),
            label=f"Before embedded apply {track_count:03d}",
            language="eng",
            origin_note="Recoverable transcript snapshot before applying embedded subtitle text",
            is_default=False,
        )
        embedded_track_path = DATA_DIR / "tracks" / f"{job_id}-embedded-{track_count + 1:03d}.srt"
        embedded_track_path.parent.mkdir(parents=True, exist_ok=True)
        embedded_track_path.write_text(render_srt(parsed_segments), encoding="utf-8")
        new_track = create_stored_track(
            job=base_job,
            source_kind="embedded-subtitle",
            format_name="srt",
            subtitle_path=str(embedded_track_path),
            label=source_label,
            language="eng",
            origin_note=f"{source_label} applied to current timing; average confidence={report.average_confidence}",
            is_default=False,
        )
        updated_job = renumber_tracks(base_job.model_copy(
            update={
                "transcript_segments": retimed_segments,
                "transcript_is_edited": True,
                "transcription_mode": "real-cli",
                "transcription_source": f"{source_label}-text-applied",
                "timing_source": base_job.timing_source,
                "subtitle_tracks": [*base_job.subtitle_tracks, snapshot_track, new_track],
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


@app.post("/jobs/{job_id}/export/mp4-softsub", response_model=None)
def export_job_softsub_mp4(
    job_id: str, payload: ExportSoftSubtitleMp4Request
) -> dict[str, str] | FileResponse:
    job = get_job(job_id)
    output_path = ensure_softsub_export_target(payload.resolved_output_path())
    export_softsub_mp4(
        job,
        output_path,
        track_ids=payload.track_ids,
    )
    artifact = register_softsub_artifact(job, output_path)
    if payload.download:
        return FileResponse(
            path=output_path,
            media_type="video/mp4",
            filename=output_path.name,
        )
    return {
        "status": "ok",
        "output_path": str(output_path),
        "message": f"Soft-subtitled MP4 exported to {output_path}",
        "artifact_id": artifact.artifact_id,
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
                "stage_label": STAGE_LABELS[next_stage],
                "stage_description": STAGE_DESCRIPTIONS[next_stage],
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

def register_softsub_artifact(job: JobDetail, output_path: Path) -> GeneratedArtifact:
    for index, existing_job in enumerate(jobs):
        if existing_job.job_id != job.job_id:
            continue
        artifact_dir = ensure_job_artifact_dir(existing_job.job_id)
        timestamp_fragment = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        artifact_path = artifact_dir / f"{existing_job.job_id}.softsub.{timestamp_fragment}.mp4"
        shutil.copy2(output_path, artifact_path)
        now = datetime.now(timezone.utc).isoformat()
        artifact_id = make_artifact_id(existing_job.job_id, len(existing_job.artifacts) + 1)
        artifact = GeneratedArtifact(
            artifact_id=artifact_id,
            artifact_kind="video-mp4-softsub",
            format_name="mp4-softsub",
            file_name=artifact_path.name,
            artifact_path=to_artifact_relative_path(artifact_path),
            size_bytes=artifact_path.stat().st_size,
            transcript_segment_count=len(existing_job.transcript_segments),
            created_at=now,
            download_url=build_artifact_download_url(existing_job.job_id, artifact_id),
        )
        updated_job = existing_job.model_copy(
            update={
                "artifacts": [*existing_job.artifacts, artifact],
                "updated_at": now,
            }
        )
        jobs[index] = updated_job
        save_state()
        return artifact
    raise HTTPException(status_code=404, detail=f"Job not found: {job.job_id}")


# ─── SCORM LMS Viewer Routes ───────────────────────────────────────────────────


SCORM_VIEWER_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SCORM Viewer{title_suffix}</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, sans-serif; background: #0f1117; color: #e5e7eb; height: 100vh; display: flex; flex-direction: column; }
  #topbar { background: #1a1d27; border-bottom: 1px solid #2d3144; padding: 10px 16px; display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
  #topbar h1 { font-size: 14px; font-weight: 600; color: #9ca3af; }
  #topbar .pkg-title { color: #f9fafb; font-size: 14px; }
  #topbar .right-controls { margin-left: auto; display: flex; align-items: center; gap: 10px; }
  .btn { font-size: 12px; padding: 5px 12px; border-radius: 999px; border: 1px solid #374151; background: #1f2937; color: #e5e7eb; cursor: pointer; }
  .btn:hover { background: #374151; }
  .btn.active { border-color: #60a5fa; color: #60a5fa; }
  .control-bar { background: #131722; border-bottom: 1px solid #2d3144; padding: 8px 16px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; flex-shrink: 0; }
  .control-group { display: flex; align-items: center; gap: 8px; }
  .control-label { font-size: 11px; color: #9ca3af; }
  .control-separator { width: 1px; align-self: stretch; background: #2d3144; }
  .range-input { accent-color: #60a5fa; }
  #seek-bar { width: min(320px, 40vw); }
  #volume-bar { width: 88px; }
  #time-readout { min-width: 96px; font-variant-numeric: tabular-nums; }
  #playback-rate { background: #111827; color: #e5e7eb; border: 1px solid #374151; border-radius: 8px; padding: 5px 8px; }
  .control-status { font-size: 11px; color: #94a3b8; }
  .cap-note { font-size: 11px; color: #94a3b8; }
  .switch { display: inline-flex; align-items: center; gap: 8px; font-size: 12px; color: #e5e7eb; }
  .switch input { position: absolute; opacity: 0; pointer-events: none; }
  .switch-track { width: 38px; height: 22px; border-radius: 999px; background: #334155; position: relative; transition: background 160ms ease; }
  .switch-track::after { content: ''; position: absolute; top: 3px; left: 3px; width: 16px; height: 16px; border-radius: 999px; background: white; transition: transform 160ms ease; }
  .switch input:checked + .switch-track { background: #2563eb; }
  .switch input:checked + .switch-track::after { transform: translateX(16px); }
  .viewer-note { font-size: 11px; color: #94a3b8; }
  #status-bar { background: #1a1d27; border-bottom: 1px solid #2d3144; padding: 5px 16px; font-size: 11px; color: #9ca3af; display: flex; gap: 16px; flex-shrink: 0; overflow-x: auto; }
  body.viewer-clean #status-bar { display: none; }
  body.viewer-clean #runtime-msg { display: none; }
  #status-bar span { white-space: nowrap; }
  #status-bar .ok { color: #34d399; }
  #status-bar .err { color: #f87171; }
  #content { flex: 1; position: relative; overflow: hidden; }
  #sco-frame { width: 100%; height: 100%; border: none; display: block; background: white; }
  #subtitle-overlay { position: absolute; bottom: 40px; left: 50%; transform: translateX(-50%); max-width: 80%; pointer-events: none; }
  .caption-line { background: rgba(0,0,0,0.75); color: #fff; font-size: 18px; padding: 4px 12px; border-radius: 4px; margin-bottom: 2px; text-align: center; line-height: 1.5; }
  #debug-panel { background: #111827; border-top: 1px solid #2d3144; color: #d1d5db; font-size: 11px; padding: 8px 12px; max-height: 180px; overflow: auto; }
  body.viewer-clean #debug-panel { display: none; }
  #debug-panel .debug-line { margin: 2px 0; word-break: break-all; }
  #debug-panel .label { color: #93c5fd; }
  #debug-panel .err { color: #fca5a5; }
  #debug-panel .ok { color: #86efac; }
</style>
</head>
<body>
<div id="topbar">
  <h1>SCORM Viewer{title_suffix}</h1>
  <span class="pkg-title">{pkg_title}</span>
  <div class="right-controls">
    <label id="cap-toggle-wrap" class="switch" style="display:none">
      <input type="checkbox" id="cap-toggle" checked>
      <span class="switch-track" aria-hidden="true"></span>
      <span>Captions</span>
    </label>
    <span class="cap-note" id="cap-note"></span>
    <span class="viewer-note" id="runtime-msg">Progress saves automatically</span>
    <button type="button" class="btn" id="show-diagnostics" style="display:none">Diagnostics</button>
  </div>
</div>
<div id="control-bar" class="control-bar">
  <div class="control-group">
    <button type="button" class="btn" id="play-toggle">Play</button>
    <button type="button" class="btn" id="seek-back">-10s</button>
    <button type="button" class="btn" id="seek-forward">+10s</button>
  </div>
  <div class="control-separator"></div>
  <div class="control-group">
    <span class="control-label">Seek</span>
    <input id="seek-bar" class="range-input" type="range" min="0" max="1000" value="0" step="1">
    <span id="time-readout" class="control-status">0:00 / 0:00</span>
  </div>
  <div class="control-separator"></div>
  <div class="control-group">
    <button type="button" class="btn" id="mute-toggle">Mute</button>
    <span class="control-label">Vol</span>
    <input id="volume-bar" class="range-input" type="range" min="0" max="1" value="1" step="0.05">
  </div>
  <div class="control-separator"></div>
  <div class="control-group">
    <span class="control-label">Speed</span>
    <select id="playback-rate">
      <option value="0.75">0.75×</option>
      <option value="1" selected>1×</option>
      <option value="1.25">1.25×</option>
      <option value="1.5">1.5×</option>
      <option value="1.75">1.75×</option>
      <option value="2">2×</option>
    </select>
    <button type="button" class="btn" id="fullscreen-toggle">Fullscreen</button>
  </div>
  <span id="control-status" class="control-status">Waiting for SCO media…</span>
</div>
<div id="status-bar">
  <span id="sb-status">Initializing...</span>
  <span id="sb-api"></span>
  <span id="sb-location"></span>
  <span id="sb-score"></span>
  <span id="sb-status2"></span>
</div>
<div id="content">
  <iframe id="sco-frame" sandbox="allow-scripts allow-same-origin allow-forms allow-popups" referrerpolicy="no-referrer"></iframe>
</div>
<div id="debug-panel">
  <div class="debug-line"><span class="label">Attempt:</span> {attempt_id}</div>
  <div class="debug-line"><span class="label">Package:</span> {package_id}</div>
  <div class="debug-line"><span class="label">API base:</span> {api_base}</div>
  <div class="debug-line"><span class="label">SCO URL:</span> {sco_url}</div>
  <div class="debug-line" id="debug-state"><span class="label">State:</span> initializing viewer</div>
  <div id="debug-log"></div>
</div>

<script>
/* ── SCORM Runtime API Shim ───────────────────────────────────────────── */
window.__SCORM_VIEWER_CONFIG = {{'attempt_id': '{attempt_id}', 'package_id': '{package_id}', 'scorm_version': '{scorm_version}', 'api_base': '{api_base}', 'captions_enabled': {captions_enabled_json}}};

const cfg = window.__SCORM_VIEWER_CONFIG;
const API_BASE = cfg.api_base;
const viewerDebug = new URLSearchParams(window.location.search).get('debug') === '1';
if (!viewerDebug) document.body.classList.add('viewer-clean');
let runtimeInitSeen = false;

function sb(id) {{ return document.getElementById(id); }}
function apiUrl(path) {{ return API_BASE + '/scorm' + path; }}
function rid() {{ return cfg.attempt_id; }}
function debug(message, cls) {{
  const root = sb('debug-log');
  if (!root) return;
  const line = document.createElement('div');
  line.className = 'debug-line' + (cls ? ' ' + cls : '');
  line.textContent = '[' + new Date().toLocaleTimeString() + '] ' + message;
  root.appendChild(line);
  root.scrollTop = root.scrollHeight;
}}
function setDebugState(message, cls) {{
  const node = sb('debug-state');
  if (!node) return;
  node.innerHTML = '<span class="label">State:</span> ' + message;
  node.className = 'debug-line' + (cls ? ' ' + cls : '');
}}

window.addEventListener('message', (event) => {{
  const data = event.data;
  if (!data || data.source !== 'subtitle-workstation-runtime') return;
  debug('SCO runtime: ' + data.message, 'ok');
}});

const diagBtn = sb('show-diagnostics');
if (diagBtn && !viewerDebug) {{
  diagBtn.style.display = '';
  diagBtn.addEventListener('click', () => {{
    document.body.classList.remove('viewer-clean');
    diagBtn.style.display = 'none';
  }});
}}

async function scormCall(method, url, body) {{
  try {{
    const res = await fetch(url, {{
      method,
      headers: {{ 'Content-Type': 'application/json' }},
      body: body ? JSON.stringify(body) : undefined,
    }});
    return await res.json();
  }} catch(e) {{
    return {{ error: String(e) }};
  }}
}}

/* Parent-window SCORM API host */
const API_12 = {{
  LMSInitialize: (p) => {{ runtimeInitSeen = true; debug('SCO called LMSInitialize', 'ok'); setDebugState('runtime initialize fired (SCORM 1.2)', 'ok'); callRT('initialize', {{}}); return 'true'; }},
  LMSFinish: (p) => {{ debug('SCO called LMSFinish', 'ok'); callRT('terminate', {{}}); return 'true'; }},
  LMSGetValue: (elem) => {{ debug('SCO called LMSGetValue(' + elem + ')'); return ''; }},
  LMSSetValue: (elem, val) => {{ debug('SCO called LMSSetValue(' + elem + ', ' + val + ')'); callRT('set', {{ element: elem, value: val }}); return 'true'; }},
  LMSCommit: (p) => {{ debug('SCO called LMSCommit', 'ok'); callRT('commit', {{}}); return 'true'; }},
  LMSGetLastError: () => '0',
  LMSGetErrorString: (n) => '',
  LMSGetDiagnostic: (n) => '',
  LMSGetVersion: () => 'SCORM_1.2',
  LMSIsInitialized: () => 'true',
}};

const API_2004 = {{
  Initialize: (p) => {{ runtimeInitSeen = true; debug('SCO called Initialize', 'ok'); setDebugState('runtime initialize fired (SCORM 2004)', 'ok'); callRT('initialize', {{}}); return 'true'; }},
  Terminate: (p) => {{ debug('SCO called Terminate', 'ok'); callRT('terminate', {{}}); return 'true'; }},
  GetValue: (elem) => {{ debug('SCO called GetValue(' + elem + ')'); return ''; }},
  SetValue: (elem, val) => {{ debug('SCO called SetValue(' + elem + ', ' + val + ')'); callRT('set', {{ element: elem, value: val }}); return 'true'; }},
  Commit: (p) => {{ debug('SCO called Commit', 'ok'); callRT('commit', {{}}); return 'true'; }},
  GetLastError: () => '0',
  GetErrorString: (n) => '',
  GetDiagnostic: (n) => '',
  GetLastErrorString: () => '',
  GetVersion: () => '1.0',
}};

window.API = API_12;
window.API_1484_11 = API_2004;
debug('Parent window exported API and API_1484_11 for SCO discovery.', 'ok');

/* Caption / subtitle injection */
function getCaptionTracks(iframe) {{
  try {{
    const doc = iframe.contentDocument || iframe.contentWindow?.document;
    if (!doc) return [];
    const videos = doc.querySelectorAll('video');
    const tracks = [];
    videos.forEach(v => v.textTracks?.forEach(t => tracks.push(t)));
    return tracks;
  }} catch(e) {{ return []; }}
}}

let _captionPollTimer = null;
let _captionObserver = null;
let _captionsWanted = !!cfg.captions_enabled;
let _primaryVideo = null;
let _primaryVideoBindings = [];
let _controlPollTimer = null;

function controlNode(id) { return sb(id); }

function formatMediaTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
  const rounded = Math.floor(seconds);
  const hours = Math.floor(rounded / 3600);
  const minutes = Math.floor((rounded % 3600) / 60);
  const secs = rounded % 60;
  if (hours > 0) return hours + ':' + String(minutes).padStart(2, '0') + ':' + String(secs).padStart(2, '0');
  return minutes + ':' + String(secs).padStart(2, '0');
}

function findScoVideos(iframe) {
  try {
    const doc = iframe.contentDocument || iframe.contentWindow?.document;
    if (!doc) return [];
    return Array.from(doc.querySelectorAll('video'));
  } catch (e) {
    return [];
  }
}

function getPrimaryVideo(iframe) {
  const videos = findScoVideos(iframe);
  if (videos.length === 0) return null;
  return videos[0];
}

function setControlStatus(message) {
  const node = controlNode('control-status');
  if (node) node.textContent = message;
}

function setControlsEnabled(enabled) {
  ['play-toggle', 'seek-back', 'seek-forward', 'seek-bar', 'mute-toggle', 'volume-bar', 'playback-rate', 'fullscreen-toggle'].forEach(id => {
    const node = controlNode(id);
    if (node) node.disabled = !enabled;
  });
}

function syncVideoControls() {
  const video = _primaryVideo;
  const playBtn = controlNode('play-toggle');
  const muteBtn = controlNode('mute-toggle');
  const seekBar = controlNode('seek-bar');
  const volumeBar = controlNode('volume-bar');
  const timeReadout = controlNode('time-readout');
  const playbackRate = controlNode('playback-rate');
  if (!playBtn || !muteBtn || !seekBar || !volumeBar || !timeReadout || !playbackRate) return;

  if (!video) {
    playBtn.textContent = 'Play';
    muteBtn.textContent = 'Mute';
    seekBar.value = '0';
    timeReadout.textContent = '0:00 / 0:00';
    playbackRate.value = '1';
    volumeBar.value = '1';
    setControlsEnabled(false);
    return;
  }

  setControlsEnabled(true);
  playBtn.textContent = video.paused ? 'Play' : 'Pause';
  muteBtn.textContent = video.muted || video.volume === 0 ? 'Unmute' : 'Mute';
  const duration = Number.isFinite(video.duration) && video.duration > 0 ? video.duration : 0;
  const currentTime = Number.isFinite(video.currentTime) ? video.currentTime : 0;
  seekBar.value = duration > 0 ? String(Math.min(1000, Math.max(0, Math.round((currentTime / duration) * 1000)))) : '0';
  timeReadout.textContent = formatMediaTime(currentTime) + ' / ' + formatMediaTime(duration);
  volumeBar.value = String(video.muted ? 0 : video.volume);
  playbackRate.value = String(video.playbackRate || 1);
}

function bindPrimaryVideo(iframe) {
  const nextVideo = getPrimaryVideo(iframe);
  if (nextVideo === _primaryVideo) {
    syncVideoControls();
    return;
  }

  _primaryVideoBindings.forEach(([eventName, handler]) => {
    try { _primaryVideo?.removeEventListener(eventName, handler); } catch (e) {}
  });
  _primaryVideoBindings = [];
  _primaryVideo = nextVideo;

  if (!_primaryVideo) {
    setControlsEnabled(false);
    setControlStatus('Waiting for SCO media…');
    syncVideoControls();
    return;
  }

  const syncHandler = () => syncVideoControls();
  ['play', 'pause', 'timeupdate', 'loadedmetadata', 'durationchange', 'volumechange', 'ratechange', 'seeking', 'seeked', 'ended'].forEach(eventName => {
    _primaryVideo.addEventListener(eventName, syncHandler);
    _primaryVideoBindings.push([eventName, syncHandler]);
  });
  setControlStatus('Wrapper controls connected to SCO video');
  syncVideoControls();
}

function setupWrapperControls(iframe) {
  const playBtn = controlNode('play-toggle');
  const backBtn = controlNode('seek-back');
  const forwardBtn = controlNode('seek-forward');
  const seekBar = controlNode('seek-bar');
  const muteBtn = controlNode('mute-toggle');
  const volumeBar = controlNode('volume-bar');
  const playbackRate = controlNode('playback-rate');
  const fullscreenBtn = controlNode('fullscreen-toggle');

  if (playBtn && !playBtn.dataset.bound) {
    playBtn.dataset.bound = '1';
    playBtn.addEventListener('click', async () => {
      if (!_primaryVideo) return;
      if (_primaryVideo.paused) await _primaryVideo.play();
      else _primaryVideo.pause();
      syncVideoControls();
    });
  }
  if (backBtn && !backBtn.dataset.bound) {
    backBtn.dataset.bound = '1';
    backBtn.addEventListener('click', () => {
      if (!_primaryVideo) return;
      _primaryVideo.currentTime = Math.max(0, (_primaryVideo.currentTime || 0) - 10);
    });
  }
  if (forwardBtn && !forwardBtn.dataset.bound) {
    forwardBtn.dataset.bound = '1';
    forwardBtn.addEventListener('click', () => {
      if (!_primaryVideo) return;
      const duration = Number.isFinite(_primaryVideo.duration) ? _primaryVideo.duration : Infinity;
      _primaryVideo.currentTime = Math.min(duration, (_primaryVideo.currentTime || 0) + 10);
    });
  }
  if (seekBar && !seekBar.dataset.bound) {
    seekBar.dataset.bound = '1';
    seekBar.addEventListener('input', (event) => {
      if (!_primaryVideo || !Number.isFinite(_primaryVideo.duration) || _primaryVideo.duration <= 0) return;
      const ratio = Number(event.target.value || 0) / 1000;
      _primaryVideo.currentTime = _primaryVideo.duration * ratio;
      syncVideoControls();
    });
  }
  if (muteBtn && !muteBtn.dataset.bound) {
    muteBtn.dataset.bound = '1';
    muteBtn.addEventListener('click', () => {
      if (!_primaryVideo) return;
      _primaryVideo.muted = !_primaryVideo.muted;
      syncVideoControls();
    });
  }
  if (volumeBar && !volumeBar.dataset.bound) {
    volumeBar.dataset.bound = '1';
    volumeBar.addEventListener('input', (event) => {
      if (!_primaryVideo) return;
      const volume = Math.max(0, Math.min(1, Number(event.target.value || 0)));
      _primaryVideo.volume = volume;
      _primaryVideo.muted = volume === 0;
      syncVideoControls();
    });
  }
  if (playbackRate && !playbackRate.dataset.bound) {
    playbackRate.dataset.bound = '1';
    playbackRate.addEventListener('change', (event) => {
      if (!_primaryVideo) return;
      const rate = Number(event.target.value || 1);
      _primaryVideo.playbackRate = rate;
      syncVideoControls();
    });
  }
  if (fullscreenBtn && !fullscreenBtn.dataset.bound) {
    fullscreenBtn.dataset.bound = '1';
    fullscreenBtn.addEventListener('click', async () => {
      const target = _primaryVideo || iframe;
      if (!target) return;
      if (document.fullscreenElement) await document.exitFullscreen();
      else if (target.requestFullscreen) await target.requestFullscreen();
    });
  }

  bindPrimaryVideo(iframe);
  window.clearInterval(_controlPollTimer);
  _controlPollTimer = window.setInterval(() => bindPrimaryVideo(iframe), 1000);
}

function applyCaptions(iframe, enabled) {{
  _captionsWanted = !!enabled;
  try {{
    const doc = iframe.contentDocument || iframe.contentWindow?.document;
    if (doc) {{
      doc.querySelectorAll('track').forEach(trackEl => {{
        if (enabled) trackEl.setAttribute('default', '');
        else trackEl.removeAttribute('default');
      }});
    }}
  }} catch(e) {{}}
  const tracks = getCaptionTracks(iframe);
  if (tracks.length === 0) {{
    sb('cap-note').textContent = enabled
      ? 'Waiting for caption tracks…'
      : 'Captions off';
    return false;
  }}
  sb('cap-note').textContent = enabled
    ? (tracks.length + ' caption track(s) on')
    : 'Captions off';
  tracks.forEach(t => {{ t.mode = enabled ? 'showing' : 'disabled'; }});
  return true;
}}

function startCaptionMonitoring(iframe) {{
  window.clearInterval(_captionPollTimer);
  try {{ _captionObserver?.disconnect(); }} catch(e) {{}}

  const poll = () => {{
    bindPrimaryVideo(iframe);
    applyCaptions(iframe, _captionsWanted);
  }};
  _captionPollTimer = window.setInterval(poll, 1000);
  poll();

  try {{
    const doc = iframe.contentDocument || iframe.contentWindow?.document;
    if (!doc) return;
    _captionObserver = new MutationObserver(() => poll());
    _captionObserver.observe(doc.documentElement || doc.body, {{ childList: true, subtree: true }});
    doc.querySelectorAll('video').forEach(video => {{
      video.addEventListener('loadedmetadata', poll);
      video.addEventListener('play', poll);
    }});
  }} catch(e) {{
    debug('Caption monitoring setup failed: ' + String(e), 'err');
  }}
}}

/* Status bar update */
let _statusTimer = null;
async function refreshStatus() {{
  try {{
    const r = await fetch(apiUrl('/attempts/' + rid() + '/runtime/status'));
    if (!r.ok) return;
    const data = await r.json();
    const rt = data.runtime_data || {};
    if (cfg.scorm_version === 'scorm2004') {{
      sb('sb-status').textContent = 'completion_status: ' + (rt['cmi.completion_status'] || 'unknown');
      sb('sb-status2').textContent = 'success_status: ' + (rt['cmi.success_status'] || 'unknown');
      sb('sb-location').textContent = 'location: ' + (rt['cmi.location'] || '');
      sb('sb-score').textContent = 'score: ' + (rt['cmi.score.raw'] || 'none');
    }} else {{
      sb('sb-status').textContent = 'lesson_status: ' + (rt['cmi.core.lesson_status'] || 'unknown');
      sb('sb-location').textContent = 'location: ' + (rt['cmi.core.lesson_location'] || '');
      sb('sb-score').textContent = 'score: ' + (rt['cmi.core.score.raw'] || 'none');
      sb('sb-status2').textContent = '';
    }}
    sb('sb-api').textContent = 'API: ' + cfg.scorm_version;
    sb('sb-api').className = 'ok';
  }} catch(e) {{}}
}}

/* Init */
async function initViewer() {{
  const scoFrame = sb('sco-frame');
  const scoUrl = '{sco_url}';
  if (!scoUrl) {{
    sb('sb-status').textContent = 'ERROR: No launch target';
    sb('sb-status').className = 'err';
    setDebugState('no launch target', 'err');
    debug('No launch target was provided by the viewer route.', 'err');
    return;
  }}

  setDebugState('loading SCO iframe', '');
  debug('Viewer loaded. API base: ' + API_BASE);
  debug('SCO URL: ' + scoUrl);
  sb('sb-status').textContent = 'Loading SCO...';

	  const initTimeout = window.setTimeout(() => {{
    if (!runtimeInitSeen) {{
      setDebugState('timeout: iframe loaded or opened, but runtime initialize never fired', 'err');
      debug('Timed out waiting for LMSInitialize/Initialize from the SCO.', 'err');
      sb('runtime-msg').textContent = 'No runtime init from SCO';
    }}
  }}, 12000);

	  scoFrame.addEventListener('load', () => {{
    sb('sb-status').textContent = 'SCO loaded';
    sb('sb-status').className = 'ok';
    setDebugState('iframe loaded; parent window API exported', 'ok');
    debug('Iframe load event fired.', 'ok');
    sb('runtime-msg').textContent = 'Parent API ready';
    sb('sb-api').textContent = 'API: parent window exported ✓';
    sb('sb-api').className = 'ok';
    /* Optional secondary injection attempt for packages that expect same-window globals */
    try {{
      const iframeWin = scoFrame.contentWindow;
      if (iframeWin && iframeWin.document) {{
        iframeWin.API = window.API;
        iframeWin.API_1484_11 = window.API_1484_11;
        debug('Mirrored API handles onto iframe window as fallback.', 'ok');
      }}
    }} catch(e) {{
      debug('Fallback API mirroring failed: ' + String(e), 'err');
    }}
	    /* Caption setup */
	    setupWrapperControls(scoFrame);
	    if (cfg.captions_enabled) {{
	      sb('cap-toggle-wrap').style.display = '';
	      sb('cap-toggle').checked = true;
	      sb('cap-toggle').addEventListener('change', e => applyCaptions(scoFrame, e.target.checked));
	      startCaptionMonitoring(scoFrame);
	    }} else {{
	      sb('cap-toggle-wrap').style.display = 'none';
	    }}
    _statusTimer = setInterval(refreshStatus, 5000);
    void refreshStatus();
  }});

  scoFrame.addEventListener('error', () => {{
    sb('sb-status').textContent = 'ERROR loading SCO';
    sb('sb-status').className = 'err';
    setDebugState('iframe error event', 'err');
    debug('Iframe error event fired while loading SCO URL.', 'err');
    window.clearTimeout(initTimeout);
  }});

  scoFrame.src = scoUrl;
  debug('Iframe src assigned.', '');
}}

window.addEventListener('DOMContentLoaded', initViewer);
window.addEventListener('error', (event) => {
  debug('window error: ' + (event.message || 'unknown error'), 'err');
});
window.addEventListener('unhandledrejection', (event) => {
  debug('unhandled rejection: ' + String(event.reason || 'unknown reason'), 'err');
});
</script>
</body>
</html>"""


def build_scorm_viewer_html(
    package_id: str,
    attempt_id: str,
    pkg_title: str,
    scorm_version: str,
    sco_url: str,
    api_base: str,
    captions_enabled: bool,
) -> str:
    title_suffix = "" if not pkg_title else f" — {escape(pkg_title)}"
    html = SCORM_VIEWER_HTML
    replacements = {
        '{title_suffix}': title_suffix,
        '{pkg_title}': escape(pkg_title),
        '{attempt_id}': attempt_id,
        '{package_id}': package_id,
        '{scorm_version}': scorm_version,
        '{sco_url}': sco_url,
        '{api_base}': api_base,
        '{captions_enabled_json}': 'true' if captions_enabled else 'false',
    }
    for needle, value in replacements.items():
        html = html.replace(needle, value)
    html = html.replace('{{', '{').replace('}}', '}')
    return html


# ── Upload ──────────────────────────────────────────────────────────────────


@app.post("/scorm/packages/upload", response_model=ScormPackageDetail)
async def upload_scorm_package(file: UploadFile = File(...)):
    """Upload a SCORM zip, validate it, extract contents, and register the package."""
    if not file.filename or not file.filename.lower().endswith('.zip'):
        raise HTTPException(status_code=400, detail="Only .zip files are accepted.")

    max_size = 5 * 1024 * 1024 * 1024  # 5 GB
    chunk_size = 1024 * 1024  # 1 MB
    total_size = 0
    package_id = make_scorm_package_id()
    upload_path = SCORM_UPLOADS_DIR / package_id
    upload_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with upload_path.open('wb') as output_file:
            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                total_size += len(chunk)
                if total_size > max_size:
                    output_file.close()
                    upload_path.unlink(missing_ok=True)
                    raise HTTPException(status_code=413, detail="Package exceeds 5 GB limit.")
                output_file.write(chunk)
    finally:
        await file.close()

    try:
        pkg = validate_and_extract_scorm_package(upload_path, package_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Validation failed: {exc}") from exc

    with scorm_lock:
        scorm_packages.append(pkg)
        save_scorm_state()
    return pkg


@app.get("/scorm/packages", response_model=list[ScormPackageSummary])
def list_scorm_packages():
    """List all uploaded SCORM packages."""
    prune_expired_scorm_data(force=True)
    return [ScormPackageSummary.model_validate(p.model_dump()) for p in scorm_packages]


@app.get("/scorm/packages/{package_id}", response_model=ScormPackageDetail)
def get_scorm_package(package_id: str):
    """Get details and validation report for a specific package."""
    return get_scorm_package_or_404(package_id)


# ── Attempts ────────────────────────────────────────────────────────────────


@app.post("/scorm/packages/{package_id}/attempts", response_model=ScormAttemptSummary)
def create_scorm_attempt(package_id: str):
    """Create a new learner attempt for a package."""
    pkg = get_scorm_package_or_404(package_id)
    if not pkg.valid:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot launch invalid package. Fix errors first.",
        )
    attempt_id = make_scorm_attempt_id()
    now = datetime.now(timezone.utc).isoformat()
    attempt = ScormAttemptSummary(
        attempt_id=attempt_id,
        package_id=package_id,
        registration_id=f"reg-{int(time.time() * 1000)}",
        launched_at=now,
        updated_at=now,
    )
    with scorm_lock:
        scorm_attempts[attempt_id] = attempt
        ensure_attempt_runtime(attempt_id)
        defaults = runtime_defaults_for_version(pkg.scorm_version)
        scorm_runtime_store[attempt_id].update(defaults)
        # register attempt on package
        for p in scorm_packages:
            if p.package_id == package_id:
                p.attempts = list(p.attempts) + [attempt_id]
                p.updated_at = now
                break
        save_scorm_state()
    return attempt


@app.get("/scorm/packages/{package_id}/attempts", response_model=list[ScormAttemptSummary])
def list_scorm_attempts(package_id: str):
    """List all attempts for a package."""
    prune_expired_scorm_data(force=True)
    _ = get_scorm_package_or_404(package_id)
    return [a for a in scorm_attempts.values() if a.package_id == package_id]


# ── Runtime API ─────────────────────────────────────────────────────────────


@app.post("/scorm/attempts/{attempt_id}/runtime/initialize")
def scorm_initialize(attempt_id: str):
    """Initialize a SCORM runtime session (LMSInitialize / Initialize)."""
    attempt = get_scorm_attempt_or_404(attempt_id)
    runtime = ensure_attempt_runtime(attempt_id)
    pkg = get_scorm_package_or_404(attempt.package_id)
    if not runtime:
        runtime.update(runtime_defaults_for_version(pkg.scorm_version))
    with scorm_lock:
        attempt.updated_at = datetime.now(timezone.utc).isoformat()
        save_scorm_state()
    return {"success": True, "runtime_data": runtime}


@app.post("/scorm/attempts/{attempt_id}/runtime/get")
def scorm_get_value(attempt_id: str, element: str = ""):
    """Get a SCORM data model element value."""
    _ = get_scorm_attempt_or_404(attempt_id)
    runtime = ensure_attempt_runtime(attempt_id)
    value = runtime.get(element, "")
    return {"value": value, "error": 0}


@app.post("/scorm/attempts/{attempt_id}/runtime/set")
def scorm_set_value(attempt_id: str, element: str = "", value: str = ""):
    """Set a SCORM data model element value."""
    attempt = get_scorm_attempt_or_404(attempt_id)
    runtime = ensure_attempt_runtime(attempt_id)
    runtime[element] = value
    # sync top-level attempt fields
    if element in (
        "cmi.core.lesson_status",
        "cmi.core.score.raw",
        "cmi.core.lesson_location",
        "cmi.suspend_data",
        "cmi.completion_status",
        "cmi.success_status",
        "cmi.score.scaled",
        "cmi.location",
        "cmi.progress_measure",
    ):
        with scorm_lock:
            attempt.updated_at = datetime.now(timezone.utc).isoformat()
            if element == "cmi.core.lesson_status" or element == "cmi.completion_status":
                attempt.completed = value in ("completed", "passed", "failed")
            if element in ("cmi.core.score.raw", "cmi.score.raw"):
                try:
                    attempt.score_raw = float(value)
                except (ValueError, TypeError):
                    pass
            if element in ("cmi.core.lesson_location", "cmi.location"):
                attempt.location = value
            if element in ("cmi.suspend_data",):
                attempt.suspend_data = value
            save_scorm_state()
    return {"success": True}


@app.post("/scorm/attempts/{attempt_id}/runtime/commit")
def scorm_commit(attempt_id: str):
    """Commit (persist) runtime data (LMSCommit / Commit)."""
    attempt = get_scorm_attempt_or_404(attempt_id)
    _ = ensure_attempt_runtime(attempt_id)
    with scorm_lock:
        attempt.updated_at = datetime.now(timezone.utc).isoformat()
        save_scorm_state()
    return {"success": True}


@app.post("/scorm/attempts/{attempt_id}/runtime/terminate")
def scorm_terminate(attempt_id: str):
    """Terminate the SCORM runtime session (LMSFinish / Terminate)."""
    attempt = get_scorm_attempt_or_404(attempt_id)
    _ = ensure_attempt_runtime(attempt_id)
    with scorm_lock:
        attempt.updated_at = datetime.now(timezone.utc).isoformat()
        save_scorm_state()
    return {"success": True}


@app.get("/scorm/attempts/{attempt_id}/runtime/status", response_model=ScormRuntimeResponse)
def scorm_runtime_status(attempt_id: str):
    """Return current runtime data for an attempt (for viewer status bar)."""
    attempt = get_scorm_attempt_or_404(attempt_id)
    runtime = ensure_attempt_runtime(attempt_id)
    return ScormRuntimeResponse(attempt=attempt, runtime_data=runtime)


# ── Viewer ─────────────────────────────────────────────────────────────────


@app.get("/scorm/packages/{package_id}/viewer")
def scorm_viewer(request: Request, package_id: str, attempt_id: str | None = None, captions: bool = True):
    """
    Return the SCORM viewer HTML page.
    If attempt_id is provided, launch into that attempt.
    If captions=true (default), attempt to enable captions in the SCO video.
    """
    pkg = get_scorm_package_or_404(package_id)
    if not pkg.valid or not pkg.launch_path:
        raise HTTPException(
            status_code=400,
            detail="Package is not valid and cannot be launched.",
        )
    # auto-create attempt if none given
    if not attempt_id:
        temp_attempt = ScormAttemptSummary(
            attempt_id=f"preview-{int(time.time() * 1000)}",
            package_id=package_id,
            registration_id=f"reg-preview-{int(time.time() * 1000)}",
            launched_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        attempt_id = temp_attempt.attempt_id
        with scorm_lock:
            scorm_attempts[attempt_id] = temp_attempt
            ensure_attempt_runtime(attempt_id)
            defaults = runtime_defaults_for_version(pkg.scorm_version)
            scorm_runtime_store[attempt_id].update(defaults)
            save_scorm_state()

    api_base = str(request.base_url).rstrip('/')
    separator = '&' if '?' in pkg.launch_path else '?'
    sco_url = (
        f"{api_base}/scorm/packages/{package_id}/content/{pkg.launch_path}"
        f"{separator}attempt_id={attempt_id}&api_base={api_base}&scorm_version={str(pkg.scorm_version or 'scorm12')}"
    )

    html = build_scorm_viewer_html(
        package_id=package_id,
        attempt_id=attempt_id,
        pkg_title=pkg.title,
        scorm_version=str(pkg.scorm_version or "scorm12"),
        sco_url=sco_url,
        api_base=api_base,
        captions_enabled=captions,
    )
    return HTMLResponse(content=html)


@app.get("/scorm/packages/{package_id}/content/{path:path}")
def serve_scorm_content(package_id: str, path: str):
    """Serve an extracted file from a SCORM package."""
    pkg = get_scorm_package_or_404(package_id)
    extract_root = Path(pkg.extracted_dir)
    # sanitize path to prevent traversal
    safe_parts = sanitize_scorm_member_name(path).parts
    target = extract_root.joinpath(*safe_parts)
    # verify target is under extract_root
    try:
        target.resolve().relative_to(extract_root.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied.")
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"Not found: {path}")
    media_type, _ = mimetypes.guess_type(str(target))
    return FileResponse(
        path=str(target),
        media_type=media_type or "application/octet-stream",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )
