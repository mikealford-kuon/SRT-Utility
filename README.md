# Subtitle Workstation (v1 Vertical Slice)

A bounded, local-first scaffold with:
- FastAPI backend (`backend/`)
- React + TypeScript frontend (`frontend/`)

This is intentionally a production-leaning **slice**, not a full product.

## Project layout

- `backend/` API scaffold with health/ingest/jobs placeholder routes
- `frontend/` minimal shell UI with workstation panels
- `ARCHITECTURE.md` current architecture and export-model notes
- `backups/` recoverable timestamped snapshots of the app

## Prerequisites

- Python 3.12 or 3.13 recommended
- Node.js 20+
- npm 10+

Note: Python 3.14 currently breaks this scaffold's pinned FastAPI/Pydantic stack on this machine because `pydantic-core` transitively hits a PyO3 compatibility ceiling. Use Python 3.13 for now.

## Run locally

### Fastest path

```bash
chmod +x start-local.sh stop-local.sh restart-local.sh
./start-local.sh
```

This launcher will:
- create `backend/.venv` if needed
- install backend requirements
- install frontend dependencies if `node_modules` is missing
- start backend on `http://127.0.0.1:8000`
- start frontend on `http://127.0.0.1:5173`
- fail fast if either port is already busy
- track both processes in `.run/`
- write logs to `.run/backend.log` and `.run/frontend.log`
- stop both when you press `Ctrl+C`

API docs: `http://127.0.0.1:8000/docs`

### Control scripts

```bash
./start-local.sh
./stop-local.sh
./restart-local.sh
```

Detached mode is also supported:

```bash
DETACH=1 ./start-local.sh
./stop-local.sh
```

### Manual path

#### 1) Backend

```bash
cd backend
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Backend base URL: `http://127.0.0.1:8000`

Health check:

```bash
curl http://127.0.0.1:8000/health
```

#### 2) Frontend

Open a second terminal:

```bash
cd frontend
npm install
npm run dev
```

Frontend URL (default): `http://127.0.0.1:5173`

## API endpoints in this slice

- `GET /health` -> service status
- `POST /ingest` -> placeholder media ingest request
- `GET /jobs` -> placeholder jobs list
- `PUT /jobs/{job_id}/transcript` -> persist edited subtitle segments
- `POST /jobs/{job_id}/retime-edited-subtitles` -> apply an edited `.srt`/`.vtt` as text authority while preserving current job timing
- `POST /jobs/{job_id}/import-subtitles` -> legacy backend-only raw timeline replacement endpoint; normal UI flows should use retiming
- `POST /jobs/{job_id}/import-sidecar-subtitles` -> apply first discovered sidecar subtitle text to current job timing
- `POST /jobs/{job_id}/import-embedded-subtitles` -> extract embedded subtitle text and apply it to current job timing
- `GET /jobs/{job_id}/export/srt` -> export current transcript as SRT
- `GET /jobs/{job_id}/export/vtt` -> export current transcript as VTT
- `GET /jobs/{job_id}/subtitle-tracks` -> list stored subtitle tracks for the job
- `POST /jobs/{job_id}/subtitle-tracks` -> create a stored subtitle track from a local `.srt` / `.vtt` path
- `DELETE /jobs/{job_id}/subtitle-tracks/{track_id}` -> remove a stored subtitle track
- `POST /jobs/{job_id}/export/mp4-softsub` -> export MP4 with one or more embedded soft subtitle tracks from stored job tracks (accepts canonical `output_path`, or compatibility pair `output_dir` + `output_filename`)

## Notes

- No Docker setup yet
- Frontend now includes the same lightweight client-side access gate used for GaryTroop. This is a friend-share gate, not server-side authentication.
- Public static builds can be published under a prefix such as `/SRT/` by setting `VITE_SERVER_BASE=/SRT/`. Set `VITE_API_BASE_URL` to a reachable FastAPI backend URL when deploying beyond the LAN.
- Current public preview shape:
  - frontend: `https://kuon.ai/SRT/`
  - API edge: `https://srt-api.kuon.ai`
  - API edge auth: nginx Basic Auth using the same access password entered in the frontend gate
  - backend/WhisperX worker: local Mac Mini FastAPI backend on port `8000`, exposed to EC2 through launchd-managed SSH reverse tunnel `com.kuon.subtitle-workstation.srt-api-tunnel`
  - EC2 role: HTTPS/nginx edge only; the existing `t3.micro` is not large enough for WhisperX processing
  - AWS GPU EC2 quota was `0` when checked, so true GPU EC2 deployment requires a quota request or another GPU host
- No database integration yet
- Placeholder jobs are persisted locally at `backend/app/data/jobs.json` and survive backend restarts
- Ingest jobs now expose explicit provenance for transcript and timing via `transcription_source` and `timing_source`
- Backend now treats local WhisperX execution as the primary automatic timing path, and the canonical local setup pins WhisperX in `backend/requirements.txt` so the backend venv provides it by default
- This path is now verified end to end on local spoken media: ingest jobs can complete with `transcription_source=real-cli:whisperx` and `timing_source=real-cli:whisperx-aligned`
- Ingest provenance distinguishes WhisperX-aligned timing (`real-cli:whisperx-aligned`) from Whisper fallback timing (`whisperx-fallback:whisper-cli-estimated|...` or `plain-whisper-cli-estimated`)
- Very short or non-speech clips may still resolve to explicit placeholder fallback like `placeholder-fallback:no-speech-detected`
- Soft-subtitle MP4 export now supports multiple embedded subtitle tracks in one MP4
- Each job now has a stored subtitle-track library used by export
- Additional tracks can be created from external `.srt` or `.vtt` files and saved into the job
- This stored-track flow is now the canonical export path for multilingual subtitle packaging
- Previous edited `.srt`/`.vtt` files can now be carried forward after a changed video: the current job supplies timing, the uploaded prior subtitle file supplies corrected text, repeated correction patterns are learned, and uncertain matches are marked for review
- Optional Claude Haiku reconciliation can be enabled in the backend with `SRT_LLM_RECONCILIATION=true`. It uses Bedrock (`BEDROCK_REGION`, `CLAUDE_MODEL_ID`) to extract structured correction candidates only; video timing remains authoritative.
- See `ARCHITECTURE.md` for the current stored-track design and next-step management improvements
- No Pyannote speaker diarization pipeline yet (still TODO)
