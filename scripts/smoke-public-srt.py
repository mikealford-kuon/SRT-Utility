#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib import error, request


API_BASE_URL = os.environ.get("SRT_API_BASE_URL", "https://srt-api.kuon.ai").rstrip("/")
PASSWORD = os.environ.get("SRT_ACCESS_PASSWORD")
USERNAME = os.environ.get("SRT_ACCESS_USER", "srt")
TEXT = os.environ.get("SRT_SMOKE_TEXT", "OpenClaw dashboard is ready for subtitle timing.")
TIMEOUT_SECONDS = int(os.environ.get("SRT_SMOKE_TIMEOUT_SECONDS", "240"))


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def auth_header() -> str:
    if not PASSWORD:
        fail("Set SRT_ACCESS_PASSWORD in the environment before running this smoke test.")
    token = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def http_json(path: str, *, method: str = "GET", data: bytes | None = None, headers: dict[str, str] | None = None) -> dict:
    req = request.Request(
        f"{API_BASE_URL}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": auth_header(),
            **(headers or {}),
        },
    )
    try:
        with request.urlopen(req, timeout=30) as response:
            payload = response.read()
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        fail(f"{method} {path} returned HTTP {exc.code}: {body[:500]}")
    except OSError as exc:
        fail(f"{method} {path} failed: {exc}")
    try:
        return json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"{method} {path} returned invalid JSON: {exc}")


def http_bytes(path: str) -> bytes:
    req = request.Request(f"{API_BASE_URL}{path}", headers={"Authorization": auth_header()})
    try:
        with request.urlopen(req, timeout=30) as response:
            return response.read()
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        fail(f"GET {path} returned HTTP {exc.code}: {body[:500]}")
    except OSError as exc:
        fail(f"GET {path} failed: {exc}")


def make_voice_wav(work_dir: Path) -> Path:
    aiff_path = work_dir / "srt-smoke.aiff"
    wav_path = work_dir / "srt-smoke.wav"
    say_binary = "/usr/bin/say"
    if not Path(say_binary).exists():
        fail("/usr/bin/say is required for this Mac-based public smoke test.")
    subprocess.run([say_binary, "-o", str(aiff_path), TEXT], check=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(aiff_path),
            "-ac",
            "1",
            "-ar",
            "16000",
            str(wav_path),
        ],
        check=True,
    )
    return wav_path


def upload_media(path: Path) -> str:
    boundary = f"----SRTSmoke{int(time.time() * 1000)}"
    file_bytes = path.read_bytes()
    body = b"".join(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            b'Content-Disposition: form-data; name="file"; filename="srt-smoke.wav"\r\n',
            b"Content-Type: audio/wav\r\n\r\n",
            file_bytes,
            b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    response = http_json(
        "/ingest/upload",
        method="POST",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    job_id = response.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        fail(f"Upload response did not include job_id: {response}")
    return job_id


def wait_for_ready(job_id: str) -> dict:
    deadline = time.time() + TIMEOUT_SECONDS
    last_payload: dict | None = None
    while time.time() < deadline:
        payload = http_json(f"/jobs/{job_id}")
        last_payload = payload
        if payload.get("stage") == "ready":
            return payload
        time.sleep(3)
    fail(f"Job {job_id} did not reach ready before timeout. Last payload: {last_payload}")


def main() -> None:
    diagnostics = http_json("/diagnostics")
    if diagnostics.get("status") != "ok":
        fail(f"Diagnostics not ok: {diagnostics}")
    if not diagnostics.get("tools", {}).get("ffmpeg", {}).get("available"):
        fail("Diagnostics says ffmpeg is unavailable.")
    if not diagnostics.get("tools", {}).get("whisperx", {}).get("available"):
        fail(f"Diagnostics says WhisperX is unavailable: {diagnostics.get('tools', {}).get('whisperx')}")

    with tempfile.TemporaryDirectory(prefix="srt-public-smoke-") as temp_dir:
        wav_path = make_voice_wav(Path(temp_dir))
        job_id = upload_media(wav_path)
        job = wait_for_ready(job_id)

    source = str(job.get("transcription_source", ""))
    timing = str(job.get("timing_source", ""))
    if "whisperx" not in source or "whisperx" not in timing:
        fail(f"Job {job_id} did not use WhisperX. source={source!r} timing={timing!r}")
    if not job.get("transcript_segments"):
        fail(f"Job {job_id} has no transcript segments.")

    artifact = http_json(
        f"/jobs/{job_id}/artifacts/build/vtt",
        method="POST",
        data=json.dumps({"output_filename": "srt-public-smoke.vtt"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    download_url = artifact.get("download_url")
    if not isinstance(download_url, str) or not download_url:
        fail(f"Artifact did not include download_url: {artifact}")
    content = http_bytes(download_url)
    if b"WEBVTT" not in content:
        fail(f"Downloaded VTT artifact did not contain WEBVTT. Size={len(content)}")

    print(
        json.dumps(
            {
                "status": "ok",
                "api": API_BASE_URL,
                "job_id": job_id,
                "transcription_source": source,
                "timing_source": timing,
                "segments": len(job.get("transcript_segments", [])),
                "artifact_id": artifact.get("artifact_id"),
                "download_bytes": len(content),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
