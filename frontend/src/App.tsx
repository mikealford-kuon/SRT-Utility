import { ChangeEvent, FormEvent, useEffect, useMemo, useRef, useState } from "react";

type PanelProps = {
  title: string;
  children: React.ReactNode;
};

const defaultApiBaseUrl = `${window.location.protocol}//${window.location.hostname}:8000`;
const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || defaultApiBaseUrl).replace(/\/+$/, "");
const ACCESS_GATE_ENABLED =
  import.meta.env.VITE_ACCESS_GATE === "true" ||
  (import.meta.env.PROD && API_BASE_URL.includes("srt-api.kuon.ai"));
const ACCESS_STORAGE_KEY = "subtitle_workstation_access";
const ACCESS_API_AUTH_KEY = "subtitle_workstation_api_auth";
const ACCESS_PASSWORD_SHA256 = "4500cb9551ecd3c1af1d917abf958866e920a8786a653897886f4d7f4bff29ab";

async function sha256Hex(value: string): Promise<string> {
  const bytes = new TextEncoder().encode(value);
  const digest = await window.crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

function hasStoredAccess(): boolean {
  try {
    return (
      window.localStorage.getItem(ACCESS_STORAGE_KEY) === "granted" &&
      window.sessionStorage.getItem(ACCESS_API_AUTH_KEY) !== null
    );
  } catch {
    return false;
  }
}

function makeBasicAuthValue(password: string): string {
  return `Basic ${window.btoa(`srt:${password}`)}`;
}

function getStoredApiAuth(): string | null {
  try {
    return window.sessionStorage.getItem(ACCESS_API_AUTH_KEY);
  } catch {
    return null;
  }
}

function storeAccess(password: string): string {
  const apiAuth = makeBasicAuthValue(password);
  try {
    window.localStorage.setItem(ACCESS_STORAGE_KEY, "granted");
    window.sessionStorage.setItem(ACCESS_API_AUTH_KEY, apiAuth);
  } catch {
    // Keep the unlock in memory when storage is unavailable.
  }
  return apiAuth;
}

function AccessGate({ onUnlock }: { onUnlock: (apiAuth: string) => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [isChecking, setIsChecking] = useState(false);

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError("");
    setIsChecking(true);

    try {
      const passwordHash = await sha256Hex(password);
      if (passwordHash === ACCESS_PASSWORD_SHA256) {
        const apiAuth = storeAccess(password);
        onUnlock(apiAuth);
        return;
      }
      setPassword("");
      setError("Incorrect password.");
    } catch {
      setError("Unable to check password in this browser.");
    } finally {
      setIsChecking(false);
    }
  };

  return (
    <main className="access-page">
      <form className="access-panel" onSubmit={onSubmit}>
        <label className="access-label" htmlFor="access-password">
          Enter Password for Access:
        </label>
        <input
          className="access-hidden-username"
          type="text"
          autoComplete="username"
          value="srt"
          readOnly
          tabIndex={-1}
          aria-hidden="true"
        />
        <input
          id="access-password"
          className="access-input"
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          autoComplete="current-password"
          autoFocus
        />
        {error ? <p className="access-error">{error}</p> : null}
        <button className="access-submit" type="submit" disabled={isChecking || password.length === 0}>
          {isChecking ? "Checking..." : "Enter"}
        </button>
      </form>
    </main>
  );
}

export default function App() {
  const [hasAccess, setHasAccess] = useState(hasStoredAccess);
  const [apiAuth, setApiAuth] = useState<string | null>(() => (hasStoredAccess() ? getStoredApiAuth() : null));

  if (!ACCESS_GATE_ENABLED) {
    return <SubtitleWorkstationApp apiAuth={null} />;
  }

  if (!hasAccess || !apiAuth) {
    return <AccessGate onUnlock={(nextApiAuth) => {
      setApiAuth(nextApiAuth);
      setHasAccess(true);
    }} />;
  }

  return <SubtitleWorkstationApp apiAuth={apiAuth} />;
}

type HealthResponse = {
  status: string;
  service: string;
  timestamp: string;
};

type JobStage = "queued" | "transcribing" | "aligned" | "diarized" | "ready";

type MediaMetadata = {
  file_name: string;
  size_bytes: number;
  duration_seconds: number | null;
  has_video: boolean | null;
  has_audio: boolean | null;
};

type JobSummary = {
  job_id: string;
  kind: string;
  media_path: string;
  media_metadata?: MediaMetadata | null;
  transcription_mode: "placeholder" | "real-cli";
  transcription_source: string;
  stage: JobStage;
  progress_percent: number;
  created_at: string;
  updated_at: string;
};

type RetimeStatus = "matched" | "low-confidence" | "new-only" | "corrected" | "sore-thumb";

type CorrectionSuggestion = {
  wrong_text: string;
  corrected_text: string;
  confidence: number;
  kind: "phrase" | "casing" | "llm-candidate";
  status: "applied" | "suggested";
  source_segment_id: string | null;
  note: string | null;
};

type TranscriptSegment = {
  segment_id: string;
  start_seconds: number;
  end_seconds: number;
  text: string;
  speaker: string | null;
  retime_confidence?: number | null;
  retime_status?: RetimeStatus | null;
  retime_note?: string | null;
  correction_suggestions?: CorrectionSuggestion[];
};

type StoredSubtitleTrack = {
  track_id: string;
  source_kind: "edited-transcript" | "uploaded-subtitle" | "sidecar-subtitle" | "embedded-subtitle" | "retimed-edits" | "pre-retime-snapshot";
  format_name: "srt" | "vtt";
  language: string;
  label: string;
  subtitle_path: string | null;
  is_default: boolean;
  is_active: boolean;
  origin_note: string | null;
  created_at: string;
  updated_at: string;
};

type GeneratedArtifact = {
  artifact_id: string;
  artifact_kind: "transcript-srt" | "transcript-vtt" | "video-mp4-softsub" | "package-scorm12" | "package-scorm2004" | "package-aicc" | "package-xapi" | "package-cmi5";
  format_name: "srt" | "vtt" | "mp4-softsub" | PackageFormat;
  file_name: string;
  artifact_path: string;
  download_url?: string | null;
  size_bytes: number;
  transcript_segment_count: number;
  created_at: string;
};

type JobDetail = JobSummary & {
  transcript_segments: TranscriptSegment[];
  transcript_is_edited: boolean;
  subtitle_tracks: StoredSubtitleTrack[];
  artifacts?: GeneratedArtifact[];
};

type IngestResponse = {
  job_id: string;
  status: "queued";
  message: string;
};

type SoftSubtitleExportResponse = {
  status: "ok";
  output_path: string;
  message: string;
  artifact_id?: string;
};

type SubtitleTrackMetadataDraft = {
  language: string;
  label: string;
  subtitle_path: string;
  is_default: boolean;
};

type SubtitleStream = {
  index: number;
  codec_name?: string;
  language?: string;
  title?: string;
};

type SidecarSubtitle = {
  path: string;
  format: "srt" | "vtt";
  file_name: string;
};

type RetimedSegmentReport = {
  segment_id: string;
  old_segment_id: string | null;
  confidence: number;
  status: RetimeStatus;
  note: string | null;
  correction_suggestions: CorrectionSuggestion[];
};

type RetimeEditedSubtitlesReport = {
  source_file_name: string;
  source_format: "srt" | "vtt";
  matched_segments: number;
  low_confidence_segments: number;
  unmatched_old_segments: number;
  unmatched_new_segments: number;
  average_confidence: number;
  threshold: number;
  created_at: string;
  learned_corrections: CorrectionSuggestion[];
  applied_corrections: number;
  sore_thumb_segments: number;
  segments: RetimedSegmentReport[];
};

type RetimeEditedSubtitlesResponse = JobDetail & {
  retime_report: RetimeEditedSubtitlesReport;
};

type PackageFormat = "scorm12" | "scorm2004" | "aicc" | "xapi" | "cmi5";
type SoftsubExportMode = "browser" | "host";

type WorkflowStepKey = "load" | "edit" | "commit" | "outputs" | "packages" | "scorm-lms";

type ScormValidationIssue = {
  level: "error" | "warning";
  message: string;
};

type ScormPackageSummary = {
  package_id: string;
  file_name: string;
  scorm_version: "scorm12" | "scorm2004" | null;
  title: string;
  launch_path: string | null;
  valid: boolean;
  uploaded_at: string;
  updated_at: string;
  issue_count: number;
};

type ScormPackageDetail = ScormPackageSummary & {
  extracted_dir: string;
  issues: ScormValidationIssue[];
  viewer_url: string | null;
  attempts: string[];
};

type ScormAttemptSummary = {
  attempt_id: string;
  package_id: string;
  learner_id: string;
  learner_name: string;
  registration_id: string;
  launched_at: string;
  updated_at: string;
  completed: boolean;
  score_raw: number | null;
  location: string | null;
  suspend_data: string | null;
};

const WORKFLOW_STEPS: Array<{ key: WorkflowStepKey; label: string; description: string }> = [
  { key: "load", label: "Source", description: "Bring in video for fresh timing" },
  { key: "edit", label: "Edit", description: "Review timing, text, and subtitle tracks" },
  { key: "commit", label: "Review", description: "Approve and save your subtitle master" },
  { key: "outputs", label: "Deliver", description: "Export SRT, VTT, and MP4 deliverables" },
  { key: "packages", label: "Packages", description: "Build LMS-ready delivery packages" },
  { key: "scorm-lms", label: "Player", description: "Upload, validate, launch, and resume SCORM packages" },
];

const WORKSPACE_SECTIONS: Array<{ label: string; steps: WorkflowStepKey[]; description: string }> = [
  { label: "Source", steps: ["load"], description: "Bring in media and generate timing" },
  { label: "Edit", steps: ["edit", "commit"], description: "Refine and save the subtitle master" },
  { label: "Deliver", steps: ["outputs", "packages", "scorm-lms"], description: "Export deliverables and launch packages" },
];

function summarizePath(path: string) {
  const normalized = path.replace(/\\/g, "/");
  const parts = normalized.split("/").filter(Boolean);
  if (parts.length === 0) {
    return path;
  }
  return parts[parts.length - 1] ?? path;
}

function stemFromFileName(fileName: string) {
  const dotIndex = fileName.lastIndexOf(".");
  return dotIndex > 0 ? fileName.slice(0, dotIndex) : fileName;
}

function resolveLocalFilePath(file: File, inputValue: string) {
  const candidatePaths = [
    (file as File & { path?: string }).path,
    (file as File & { webkitRelativePath?: string }).webkitRelativePath,
    inputValue,
  ];

  for (const candidate of candidatePaths) {
    if (!candidate) {
      continue;
    }
    if (candidate.startsWith("C:\\fakepath\\")) {
      continue;
    }
    return candidate;
  }

  return null;
}

function displayPathForSelectedFile(file: File, inputValue: string) {
  return resolveLocalFilePath(file, inputValue) ?? file.name;
}

function isFakeBrowserPath(value: string) {
  return value.startsWith("C:\\fakepath\\");
}

function Panel({ title, children }: PanelProps) {
  return (
    <section className="panel">
      <header className="panel-header">{title}</header>
      <div className="panel-body">{children}</div>
    </section>
  );
}

function formatTimestamp(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString();
}

function formatBytes(value: number) {
  if (!Number.isFinite(value) || value < 0) {
    return String(value);
  }
  if (value < 1024) {
    return `${value} B`;
  }
  const units = ["KB", "MB", "GB", "TB"];
  let current = value / 1024;
  let unitIndex = 0;
  while (current >= 1024 && unitIndex < units.length - 1) {
    current /= 1024;
    unitIndex += 1;
  }
  return `${current.toFixed(current >= 10 ? 1 : 2)} ${units[unitIndex]}`;
}

function formatDuration(seconds: number | null) {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) {
    return "Unknown";
  }
  return `${seconds.toFixed(2)}s`;
}

function formatTimecode(totalSeconds: number) {
  if (!Number.isFinite(totalSeconds) || totalSeconds < 0) {
    return "00:00.000";
  }
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds - minutes * 60;
  return `${String(minutes).padStart(2, "0")}:${seconds.toFixed(3).padStart(6, "0")}`;
}

async function readErrorMessage(response: Response) {
  try {
    const payload = (await response.json()) as { detail?: unknown };
    const detail = payload.detail;
    if (typeof detail === "string" && detail.length > 0) {
      return detail;
    }
  } catch {
    // ignore parse failures and use status fallback below
  }
  return `Request failed (${response.status})`;
}

function transcriptSegmentsAreEqual(
  left: TranscriptSegment[],
  right: TranscriptSegment[],
) {
  if (left.length !== right.length) {
    return false;
  }
  return left.every((segment, index) => {
    const target = right[index];
    if (!target) {
      return false;
    }
    return (
      segment.segment_id === target.segment_id &&
      segment.start_seconds === target.start_seconds &&
      segment.end_seconds === target.end_seconds &&
      segment.text === target.text &&
      segment.speaker === target.speaker
    );
  });
}

function downloadBlobFile(blob: Blob, fileName: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = fileName;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function triggerBrowserDownload(url: string) {
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.target = "_blank";
  anchor.rel = "noopener";
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
}

function formatRelativeTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  const deltaMs = Date.now() - date.getTime();
  const tense = deltaMs >= 0 ? "ago" : "from now";
  const absSeconds = Math.round(Math.abs(deltaMs) / 1000);
  if (absSeconds < 10) return "just now";
  if (absSeconds < 60) return `${absSeconds}s ${tense}`;
  const absMinutes = Math.round(absSeconds / 60);
  if (absMinutes < 60) return `${absMinutes}m ${tense}`;
  const absHours = Math.round(absMinutes / 60);
  if (absHours < 24) return `${absHours}h ${tense}`;
  const absDays = Math.round(absHours / 24);
  return `${absDays}d ${tense}`;
}

function resolveArtifactDownloadUrl(jobId: string, artifact: GeneratedArtifact): string {
  if (artifact.download_url && artifact.download_url.trim()) {
    if (artifact.download_url.startsWith("http")) {
      return artifact.download_url;
    }
    return artifact.download_url.startsWith("/")
      ? `${API_BASE_URL}${artifact.download_url}`
      : `${API_BASE_URL}/${artifact.download_url}`;
  }
  return `${API_BASE_URL}/jobs/${jobId}/artifacts/${artifact.artifact_id}/download`;
}

function SubtitleWorkstationApp({ apiAuth }: { apiAuth: string | null }) {
  const [backendStatus, setBackendStatus] = useState("checking...");
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [selectedJobDetail, setSelectedJobDetail] = useState<JobDetail | null>(null);
  const [jobDetailError, setJobDetailError] = useState("");
  const [isLoadingJobDetail, setIsLoadingJobDetail] = useState(false);
  const [mediaPath, setMediaPath] = useState("");
  const [ingestMessage, setIngestMessage] = useState("");
  const [ingestMessageIsError, setIngestMessageIsError] = useState(false);
  const [advancingJobId, setAdvancingJobId] = useState<string | null>(null);
  const [segmentDrafts, setSegmentDrafts] = useState<TranscriptSegment[]>([]);
  const [expandedSegmentId, setExpandedSegmentId] = useState<string | null>(null);
  const [isSavingSegments, setIsSavingSegments] = useState(false);
  const [segmentSaveMessage, setSegmentSaveMessage] = useState("");
  const [segmentSaveMessageIsError, setSegmentSaveMessageIsError] = useState(false);
  const [exportMessage, setExportMessage] = useState("");
  const [exportMessageIsError, setExportMessageIsError] = useState(false);
  const [exportingFormat, setExportingFormat] = useState<"srt" | "vtt" | "mp4-softsub" | PackageFormat | null>(null);
  const [selectedFileName, setSelectedFileName] = useState("");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [isSubmittingIngest, setIsSubmittingIngest] = useState(false);
  const [softsubOutputDir, setSoftsubOutputDir] = useState("");
  const [exportBaseName, setExportBaseName] = useState("");
  const [softsubOutputName, setSoftsubOutputName] = useState("");
  const [softsubExportMode, setSoftsubExportMode] = useState<SoftsubExportMode>("browser");
  const [legacySubtitleFile, setLegacySubtitleFile] = useState<File | null>(null);
  const [legacySubtitleFileName, setLegacySubtitleFileName] = useState("");
  const [legacySubtitlePath, setLegacySubtitlePath] = useState("");
  const [subtitleImportFileName, setSubtitleImportFileName] = useState("");
  const [isRetimingSubtitles, setIsRetimingSubtitles] = useState(false);
  const [retimeReport, setRetimeReport] = useState<RetimeEditedSubtitlesReport | null>(null);
  const [showLowConfidenceOnly, setShowLowConfidenceOnly] = useState(false);
  const [softsubLanguage, setSoftsubLanguage] = useState("eng");
  const [softsubLabel, setSoftsubLabel] = useState("English Subtitles");
  const [additionalTrackDrafts, setAdditionalTrackDrafts] = useState<SubtitleTrackMetadataDraft[]>([]);
  const [subtitleStreams, setSubtitleStreams] = useState<SubtitleStream[]>([]);
  const [selectedSubtitleStreamIndex, setSelectedSubtitleStreamIndex] = useState("");
  const [isImportingEmbeddedSubtitles, setIsImportingEmbeddedSubtitles] = useState(false);
  const [sidecarSubtitles, setSidecarSubtitles] = useState<SidecarSubtitle[]>([]);
  const [isImportingSidecarSubtitles, setIsImportingSidecarSubtitles] = useState(false);
  const [isCreatingTrack, setIsCreatingTrack] = useState(false);
  const [artifactActionKey, setArtifactActionKey] = useState<string | null>(null);
  const [activeStep, setActiveStep] = useState<WorkflowStepKey>("load");
  const [scormPackages, setScormPackages] = useState<ScormPackageSummary[]>([]);
  const [selectedScormPackageId, setSelectedScormPackageId] = useState<string | null>(null);
  const [selectedScormPackage, setSelectedScormPackage] = useState<ScormPackageDetail | null>(null);
  const [scormAttempts, setScormAttempts] = useState<ScormAttemptSummary[]>([]);
  const [scormFile, setScormFile] = useState<File | null>(null);
  const [scormFileName, setScormFileName] = useState("");
  const [isUploadingScorm, setIsUploadingScorm] = useState(false);
  const [isLaunchingScorm, setIsLaunchingScorm] = useState<string | null>(null);
  const [scormMessage, setScormMessage] = useState("");
  const [scormMessageIsError, setScormMessageIsError] = useState(false);
  const mediaFileInputRef = useRef<HTMLInputElement | null>(null);
  const legacySubtitleInputRef = useRef<HTMLInputElement | null>(null);

  const apiFetch = (input: RequestInfo | URL, init: RequestInit = {}) => {
    const headers = new Headers(init.headers);
    if (apiAuth) {
      headers.set("Authorization", apiAuth);
    }
    return window.fetch(input, {
      ...init,
      headers,
    });
  };

  const downloadFromApi = async (url: string, fileName: string) => {
    const response = await apiFetch(url);
    if (!response.ok) {
      throw new Error(await readErrorMessage(response));
    }
    const blob = await response.blob();
    downloadBlobFile(blob, fileName);
  };

  const loadJobs = async () => {
    const response = await apiFetch(`${API_BASE_URL}/jobs`);
    if (!response.ok) {
      throw new Error(`jobs request failed (${response.status})`);
    }
    const data = (await response.json()) as JobSummary[];
    setJobs(data);
  };

  const loadScormPackages = async () => {
    const response = await apiFetch(`${API_BASE_URL}/scorm/packages`);
    if (!response.ok) {
      throw new Error(`scorm packages request failed (${response.status})`);
    }
    const data = (await response.json()) as ScormPackageSummary[];
    setScormPackages(data);
  };

  const loadScormPackageDetail = async (packageId: string) => {
    const [pkgResponse, attemptsResponse] = await Promise.all([
      apiFetch(`${API_BASE_URL}/scorm/packages/${packageId}`),
      apiFetch(`${API_BASE_URL}/scorm/packages/${packageId}/attempts`),
    ]);
    if (!pkgResponse.ok) {
      throw new Error(await readErrorMessage(pkgResponse));
    }
    if (!attemptsResponse.ok) {
      throw new Error(await readErrorMessage(attemptsResponse));
    }
    const pkg = (await pkgResponse.json()) as ScormPackageDetail;
    const attempts = (await attemptsResponse.json()) as ScormAttemptSummary[];
    setSelectedScormPackage(pkg);
    setScormAttempts(attempts);
  };

  useEffect(() => {
    const loadData = async () => {
      try {
        const healthResponse = await apiFetch(`${API_BASE_URL}/health`);
        if (!healthResponse.ok) {
          throw new Error(`health request failed (${healthResponse.status})`);
        }
        const health = (await healthResponse.json()) as HealthResponse;
        setBackendStatus(`${health.status} (${health.service})`);
      } catch {
        setBackendStatus("offline");
      }

      try {
        await Promise.all([loadJobs(), loadScormPackages()]);
      } catch {
        setIngestMessageIsError(true);
        setIngestMessage("Failed to load jobs from backend.");
      }
    };
    void loadData();
  }, []);

  useEffect(() => {
    if (jobs.length === 0) {
      setSelectedJobId(null);
      return;
    }
    if (!selectedJobId || !jobs.some((job) => job.job_id === selectedJobId)) {
      const preferredJob = [...jobs]
        .filter((job) => !job.job_id.includes("temp-artifact-check"))
        .sort((left, right) => new Date(right.updated_at).getTime() - new Date(left.updated_at).getTime())[0] ?? jobs[0];
      setSelectedJobId(preferredJob.job_id);
    }
  }, [jobs, selectedJobId]);

  useEffect(() => {
    if (scormPackages.length === 0) {
      setSelectedScormPackageId(null);
      setSelectedScormPackage(null);
      setScormAttempts([]);
      return;
    }
    if (!selectedScormPackageId || !scormPackages.some((pkg) => pkg.package_id === selectedScormPackageId)) {
      const preferredPackage = [...scormPackages].sort(
        (left, right) => new Date(right.updated_at).getTime() - new Date(left.updated_at).getTime(),
      )[0] ?? scormPackages[0];
      setSelectedScormPackageId(preferredPackage.package_id);
    }
  }, [scormPackages, selectedScormPackageId]);

  useEffect(() => {
    if (!selectedScormPackageId) {
      setSelectedScormPackage(null);
      setScormAttempts([]);
      return;
    }
    void loadScormPackageDetail(selectedScormPackageId).catch((error) => {
      setSelectedScormPackage(null);
      setScormAttempts([]);
      setScormMessage(error instanceof Error ? error.message : "Failed to load SCORM package detail.");
      setScormMessageIsError(true);
    });
  }, [selectedScormPackageId]);

  const selectedJob = useMemo(
    () => jobs.find((job) => job.job_id === selectedJobId) ?? null,
    [jobs, selectedJobId],
  );

  useEffect(() => {
    if (!selectedJobId) {
      setSelectedJobDetail(null);
      setJobDetailError("");
      setIsLoadingJobDetail(false);
      return;
    }

    let isActive = true;
    const loadJobDetail = async (showLoading = true) => {
      if (showLoading) {
        setIsLoadingJobDetail(true);
      }
      setJobDetailError("");
      try {
        const response = await apiFetch(`${API_BASE_URL}/jobs/${selectedJobId}`);
        if (!response.ok) {
          throw new Error(await readErrorMessage(response));
        }
        const data = (await response.json()) as JobDetail;
        if (!isActive) {
          return;
        }
        setSelectedJobDetail(data);
      } catch (error) {
        if (!isActive) {
          return;
        }
        setSelectedJobDetail(null);
        setJobDetailError(
          error instanceof Error ? error.message : "Failed to load job detail.",
        );
      } finally {
        if (isActive && showLoading) {
          setIsLoadingJobDetail(false);
        }
      }
    };

    void loadJobDetail();
    const intervalId = window.setInterval(() => {
      void loadJobDetail(false);
      void loadJobs().catch(() => undefined);
    }, 3000);

    return () => {
      isActive = false;
      window.clearInterval(intervalId);
    };
  }, [selectedJobId]);

  useEffect(() => {
    setSegmentDrafts(selectedJobDetail?.transcript_segments ?? []);
    setExpandedSegmentId(selectedJobDetail?.transcript_segments?.[0]?.segment_id ?? null);
    setSegmentSaveMessage("");
    setSegmentSaveMessageIsError(false);
    setExportMessage("");
    setExportMessageIsError(false);
  }, [selectedJobDetail?.job_id, selectedJobDetail?.updated_at]);

  useEffect(() => {
    setRetimeReport(null);
    setShowLowConfidenceOnly(false);
  }, [selectedJobDetail?.job_id]);

  const editorJob = selectedJobDetail ?? selectedJob;

  useEffect(() => {
    if (!editorJob) {
      setSoftsubOutputDir("");
      setSoftsubOutputName("");
      setSoftsubExportMode("browser");
      setSoftsubLanguage("eng");
      setSoftsubLabel("English Subtitles");
      setAdditionalTrackDrafts([]);
      return;
    }
    const mediaPathValue = editorJob.media_path;
    const lastSlashIndex = Math.max(mediaPathValue.lastIndexOf("/"), mediaPathValue.lastIndexOf("\\"));
    const directory = lastSlashIndex >= 0 ? mediaPathValue.slice(0, lastSlashIndex) : "";
    const baseName = editorJob.media_metadata?.file_name ?? `${editorJob.job_id}.mp4`;
    const stem = stemFromFileName(baseName);
    setSoftsubOutputDir(directory);
    setExportBaseName(stem);
    setSoftsubOutputName(`${stem}.softsubs.mp4`);
    setSoftsubExportMode("browser");
    setSoftsubLanguage("eng");
    setSoftsubLabel("English Subtitles");
    setAdditionalTrackDrafts([]);
  }, [editorJob?.job_id]);
  const transcriptSegments = segmentDrafts;
  const visibleTranscriptSegments = showLowConfidenceOnly
    ? transcriptSegments.filter((segment) => segment.retime_status === "low-confidence")
    : transcriptSegments;
  const canSubmitIngest = Boolean(selectedFile || mediaPath.trim());
  const hasUnsavedSegmentChanges = useMemo(() => {
    if (!selectedJobDetail) {
      return false;
    }
    return !transcriptSegmentsAreEqual(
      selectedJobDetail.transcript_segments,
      transcriptSegments,
    );
  }, [selectedJobDetail, transcriptSegments]);

  const onIngest = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setIngestMessage("");
    setIngestMessageIsError(false);
    setIsSubmittingIngest(true);

    try {
      let response: globalThis.Response;
      if (selectedFile) {
        const formData = new FormData();
        formData.append("file", selectedFile);
        if (legacySubtitleFile) {
          formData.append("legacy_subtitle", legacySubtitleFile);
        } else if (legacySubtitlePath.trim() && !isFakeBrowserPath(legacySubtitlePath.trim())) {
          formData.append("legacy_subtitle_path", legacySubtitlePath.trim());
        }
        if (mediaPath.trim() && !isFakeBrowserPath(mediaPath.trim())) {
          formData.append("original_path", mediaPath.trim());
        }
        response = await apiFetch(`${API_BASE_URL}/ingest/upload`, {
          method: "POST",
          body: formData,
        });
      } else {
        response = await apiFetch(`${API_BASE_URL}/ingest`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            media_path: mediaPath,
            legacy_subtitle_path: legacySubtitlePath.trim() || null,
          }),
        });
      }
      if (!response.ok) {
        throw new Error(await readErrorMessage(response));
      }
      const data = (await response.json()) as IngestResponse;
      setIngestMessageIsError(false);
      setIngestMessage(data.message);
      await loadJobs();
      setSelectedJobId(data.job_id);
      setMediaPath("");
      setSelectedFileName("");
      setSelectedFile(null);
      setLegacySubtitleFile(null);
      setLegacySubtitleFileName("");
      setLegacySubtitlePath("");
    } catch (error) {
      setIngestMessageIsError(true);
      setIngestMessage(
        error instanceof Error ? error.message : "Failed to queue ingest.",
      );
    } finally {
      setIsSubmittingIngest(false);
    }
  };

  const onSelectLocalFile = (event: ChangeEvent<HTMLInputElement>) => {
    const nextSelectedFile = event.target.files?.[0];
    if (!nextSelectedFile) {
      setSelectedFile(null);
      setSelectedFileName("");
      setLegacySubtitleFile(null);
      setLegacySubtitleFileName("");
      return;
    }

    setSelectedFile(nextSelectedFile);
    setSelectedFileName(nextSelectedFile.name);
    setMediaPath(displayPathForSelectedFile(nextSelectedFile, event.target.value));
    setIngestMessage("");
    setIngestMessageIsError(false);
  };

  const onSelectLegacySubtitleFile = (event: ChangeEvent<HTMLInputElement>) => {
    const nextSubtitleFile = event.target.files?.[0] ?? null;
    setLegacySubtitleFile(nextSubtitleFile);
    setLegacySubtitleFileName(nextSubtitleFile?.name ?? "");
    if (nextSubtitleFile) {
      setLegacySubtitlePath(displayPathForSelectedFile(nextSubtitleFile, event.target.value));
    }
    setIngestMessage("");
    setIngestMessageIsError(false);
  };

  const onApplyPreviousEditsFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const subtitleFile = event.target.files?.[0];
    if (!subtitleFile || !selectedJobDetail) {
      return;
    }

    setIsRetimingSubtitles(true);
    setSubtitleImportFileName(subtitleFile.name);
    setSegmentSaveMessage("");
    setSegmentSaveMessageIsError(false);
    try {
      const formData = new FormData();
      formData.append("file", subtitleFile);
      formData.append("confidence_threshold", "0.58");
      const response = await apiFetch(
        `${API_BASE_URL}/jobs/${selectedJobDetail.job_id}/retime-edited-subtitles`,
        {
          method: "POST",
          body: formData,
        },
      );
      if (!response.ok) {
        throw new Error(await readErrorMessage(response));
      }
      const data = (await response.json()) as RetimeEditedSubtitlesResponse;
      setSelectedJobDetail(data);
      setSegmentDrafts(data.transcript_segments);
      setRetimeReport(data.retime_report);
      setShowLowConfidenceOnly(data.retime_report.low_confidence_segments > 0);
      await loadJobs();
      setSegmentSaveMessage(
        `Applied and adjusted ${subtitleFile.name}: ${data.retime_report.matched_segments} matched, ${data.retime_report.low_confidence_segments} need review.`,
      );
      setSegmentSaveMessageIsError(false);
    } catch (error) {
      setSegmentSaveMessage(
        error instanceof Error ? error.message : "Failed to apply previous subtitle edits.",
      );
      setSegmentSaveMessageIsError(true);
    } finally {
      event.target.value = "";
      setIsRetimingSubtitles(false);
    }
  };

  const onImportEmbeddedSubtitles = async () => {
    if (!selectedJobDetail) {
      return;
    }

    setIsImportingEmbeddedSubtitles(true);
    setSegmentSaveMessage("");
    setSegmentSaveMessageIsError(false);
    try {
      const response = await apiFetch(
        `${API_BASE_URL}/jobs/${selectedJobDetail.job_id}/import-embedded-subtitles`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            stream_index: selectedSubtitleStreamIndex
              ? Number.parseInt(selectedSubtitleStreamIndex, 10)
              : null,
          }),
        },
      );
      if (!response.ok) {
        throw new Error(await readErrorMessage(response));
      }
      const data = (await response.json()) as JobDetail;
      setSelectedJobDetail(data);
      setSegmentDrafts(data.transcript_segments);
      setRetimeReport(null);
      setShowLowConfidenceOnly(false);
      await loadJobs();
      setSegmentSaveMessage("Applied embedded subtitle text to the current video timing.");
      setSegmentSaveMessageIsError(false);
    } catch (error) {
      setSegmentSaveMessage(
        error instanceof Error ? error.message : "Failed to import embedded subtitles.",
      );
      setSegmentSaveMessageIsError(true);
    } finally {
      setIsImportingEmbeddedSubtitles(false);
    }
  };

  const onImportSidecarSubtitles = async () => {
    if (!selectedJobDetail) {
      return;
    }

    setIsImportingSidecarSubtitles(true);
    setSegmentSaveMessage("");
    setSegmentSaveMessageIsError(false);
    try {
      const response = await apiFetch(
        `${API_BASE_URL}/jobs/${selectedJobDetail.job_id}/import-sidecar-subtitles`,
        {
          method: "POST",
        },
      );
      if (!response.ok) {
        throw new Error(await readErrorMessage(response));
      }
      const data = (await response.json()) as JobDetail;
      setSelectedJobDetail(data);
      setSegmentDrafts(data.transcript_segments);
      setRetimeReport(null);
      setShowLowConfidenceOnly(false);
      await loadJobs();
      setSegmentSaveMessage("Applied sidecar subtitle text to the current video timing.");
      setSegmentSaveMessageIsError(false);
    } catch (error) {
      setSegmentSaveMessage(
        error instanceof Error ? error.message : "Failed to import sidecar subtitles.",
      );
      setSegmentSaveMessageIsError(true);
    } finally {
      setIsImportingSidecarSubtitles(false);
    }
  };

  const onAdvanceJob = async (jobId: string) => {
    setIngestMessage("");
    setIngestMessageIsError(false);
    setAdvancingJobId(jobId);
    try {
      const response = await apiFetch(`${API_BASE_URL}/jobs/${jobId}/advance`, {
        method: "POST",
      });
      if (!response.ok) {
        throw new Error(await readErrorMessage(response));
      }
      await loadJobs();
    } catch (error) {
      setIngestMessageIsError(true);
      setIngestMessage(
        error instanceof Error
          ? error.message
          : `Failed to advance job ${jobId}.`,
      );
    } finally {
      setAdvancingJobId(null);
    }
  };

  const onSegmentNumberChange = (
    index: number,
    key: "start_seconds" | "end_seconds",
    rawValue: string,
  ) => {
    const parsedValue = Number.parseFloat(rawValue);
    if (!Number.isFinite(parsedValue)) {
      return;
    }
    setSegmentDrafts((current) =>
      current.map((segment, currentIndex) =>
        currentIndex === index ? { ...segment, [key]: parsedValue } : segment,
      ),
    );
  };

  const onSegmentTextChange = (index: number, text: string) => {
    setSegmentDrafts((current) =>
      current.map((segment, currentIndex) =>
        currentIndex === index ? { ...segment, text } : segment,
      ),
    );
  };

  const onSegmentSpeakerChange = (index: number, speakerRaw: string) => {
    setSegmentDrafts((current) =>
      current.map((segment, currentIndex) =>
        currentIndex === index
          ? { ...segment, speaker: speakerRaw.trim() ? speakerRaw : null }
          : segment,
      ),
    );
  };

  const saveTranscriptSegments = async (): Promise<JobDetail> => {
    if (!selectedJobDetail) {
      throw new Error("No job selected.");
    }
    const response = await apiFetch(
      `${API_BASE_URL}/jobs/${selectedJobDetail.job_id}/transcript`,
      {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ transcript_segments: segmentDrafts }),
      },
    );
    if (!response.ok) {
      throw new Error(await readErrorMessage(response));
    }
    const data = (await response.json()) as JobDetail;
    setSelectedJobDetail(data);
    setSegmentDrafts(data.transcript_segments);
    await loadJobs();
    return data;
  };

  const ensureTranscriptSavedForExport = async () => {
    if (!selectedJobDetail || !hasUnsavedSegmentChanges) {
      return selectedJobDetail;
    }
    setIsSavingSegments(true);
    setSegmentSaveMessage("Saving transcript edits before export...");
    setSegmentSaveMessageIsError(false);
    try {
      const data = await saveTranscriptSegments();
      setSegmentSaveMessage("Subtitle changes saved.");
      setSegmentSaveMessageIsError(false);
      return data;
    } catch (error) {
      setSegmentSaveMessage(
        error instanceof Error ? error.message : "Failed to save subtitle changes.",
      );
      setSegmentSaveMessageIsError(true);
      throw error;
    } finally {
      setIsSavingSegments(false);
    }
  };

  const onSaveSegments = async () => {
    if (!selectedJobDetail) {
      return;
    }
    setIsSavingSegments(true);
    setSegmentSaveMessage("");
    setSegmentSaveMessageIsError(false);
    try {
      await saveTranscriptSegments();
      setSegmentSaveMessage("Subtitle changes saved.");
      setSegmentSaveMessageIsError(false);
    } catch (error) {
      setSegmentSaveMessage(
        error instanceof Error ? error.message : "Failed to save subtitle changes.",
      );
      setSegmentSaveMessageIsError(true);
    } finally {
      setIsSavingSegments(false);
    }
  };

  const refreshSelectedJobDetail = async () => {
    if (!editorJob) {
      return;
    }
    const response = await apiFetch(`${API_BASE_URL}/jobs/${editorJob.job_id}`);
    if (response.ok) {
      const data = (await response.json()) as JobDetail;
      setSelectedJobDetail(data);
    }
  };

  const onDownloadArtifact = async (artifact: GeneratedArtifact) => {
    if (!editorJob) {
      return;
    }
    setArtifactActionKey(`download-${artifact.artifact_id}`);
    setExportMessage("");
    setExportMessageIsError(false);
    try {
      await downloadFromApi(resolveArtifactDownloadUrl(editorJob.job_id, artifact), artifact.file_name);
      setExportMessage(`Downloaded ${artifact.file_name}.`);
      setExportMessageIsError(false);
    } catch (error) {
      setExportMessage(
        error instanceof Error ? error.message : `Failed to download ${artifact.file_name}.`,
      );
      setExportMessageIsError(true);
    } finally {
      setArtifactActionKey(null);
    }
  };

  const onExport = async (format: "srt" | "vtt") => {
    if (!editorJob) {
      return;
    }
    const requestedBaseName = exportBaseName.trim();
    setExportingFormat(format);
    setExportMessage("");
    setExportMessageIsError(false);
    try {
      await ensureTranscriptSavedForExport();
      const response = await apiFetch(`${API_BASE_URL}/jobs/${editorJob.job_id}/artifacts/build/${format}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          output_filename: requestedBaseName ? `${requestedBaseName}.${format}` : null,
        }),
      });
      if (!response.ok) {
        throw new Error(await readErrorMessage(response));
      }
      const artifact = (await response.json()) as GeneratedArtifact;
      await refreshSelectedJobDetail();
      await onDownloadArtifact(artifact);
    } catch (error) {
      setExportMessage(
        error instanceof Error
          ? error.message
          : `Failed to export ${format.toUpperCase()}.`,
      );
      setExportMessageIsError(true);
    } finally {
      setExportingFormat(null);
    }
  };

  const onAdditionalTrackChange = (
    index: number,
    key: keyof SubtitleTrackMetadataDraft,
    value: string | boolean,
  ) => {
    setAdditionalTrackDrafts((current) =>
      current.map((track, currentIndex) =>
        currentIndex === index ? { ...track, [key]: value } : track,
      ),
    );
  };

  const onAddAdditionalTrack = () => {
    setAdditionalTrackDrafts((current) => [
      ...current,
      {
        language: "spa",
        label: "Spanish Subtitles",
        subtitle_path: "",
        is_default: false,
      },
    ]);
  };

  const onRemoveAdditionalTrack = (index: number) => {
    setAdditionalTrackDrafts((current) => current.filter((_, currentIndex) => currentIndex !== index));
  };

  const onCreateStoredTrack = async (track: SubtitleTrackMetadataDraft) => {
    if (!selectedJobDetail) {
      return;
    }
    setIsCreatingTrack(true);
    setSegmentSaveMessage("");
    setSegmentSaveMessageIsError(false);
    try {
      const response = await apiFetch(`${API_BASE_URL}/jobs/${selectedJobDetail.job_id}/subtitle-tracks`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          language: track.language.trim() || "eng",
          label: track.label.trim() || "Subtitle Track",
          subtitle_path: track.subtitle_path.trim(),
          is_default: Boolean(track.is_default),
        }),
      });
      if (!response.ok) {
        throw new Error(await readErrorMessage(response));
      }
      const data = (await response.json()) as JobDetail;
      setSelectedJobDetail(data);
      await loadJobs();
      setSegmentSaveMessage(`Stored subtitle track: ${track.label || track.subtitle_path}`);
      setSegmentSaveMessageIsError(false);
    } catch (error) {
      setSegmentSaveMessage(
        error instanceof Error ? error.message : "Failed to create subtitle track.",
      );
      setSegmentSaveMessageIsError(true);
    } finally {
      setIsCreatingTrack(false);
    }
  };

  const onDeleteStoredTrack = async (trackId: string) => {
    if (!selectedJobDetail) {
      return;
    }
    setIsCreatingTrack(true);
    setSegmentSaveMessage("");
    setSegmentSaveMessageIsError(false);
    try {
      const response = await apiFetch(`${API_BASE_URL}/jobs/${selectedJobDetail.job_id}/subtitle-tracks/${trackId}`, {
        method: "DELETE",
      });
      if (!response.ok) {
        throw new Error(await readErrorMessage(response));
      }
      const data = (await response.json()) as JobDetail;
      setSelectedJobDetail(data);
      await loadJobs();
      setSegmentSaveMessage(`Removed subtitle track ${trackId}.`);
      setSegmentSaveMessageIsError(false);
    } catch (error) {
      setSegmentSaveMessage(
        error instanceof Error ? error.message : "Failed to delete subtitle track.",
      );
      setSegmentSaveMessageIsError(true);
    } finally {
      setIsCreatingTrack(false);
    }
  };

  const onSelectScormFile = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0] ?? null;
    setScormFile(file);
    setScormFileName(file?.name ?? "");
  };

  const onUploadScorm = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!scormFile) {
      setScormMessage("Choose a SCORM .zip first.");
      setScormMessageIsError(true);
      return;
    }
    setIsUploadingScorm(true);
    setScormMessage("");
    setScormMessageIsError(false);
    try {
      const formData = new FormData();
      formData.append("file", scormFile);
      const response = await apiFetch(`${API_BASE_URL}/scorm/packages/upload`, {
        method: "POST",
        body: formData,
      });
      if (!response.ok) {
        throw new Error(await readErrorMessage(response));
      }
      const created = (await response.json()) as ScormPackageDetail;
      await loadScormPackages();
      setSelectedScormPackageId(created.package_id);
      setScormMessage(`Uploaded ${created.file_name}${created.valid ? " and validation passed" : " with validation issues"}.`);
      setScormMessageIsError(false);
      setScormFile(null);
      setScormFileName("");
    } catch (error) {
      setScormMessage(error instanceof Error ? error.message : "Failed to upload SCORM package.");
      setScormMessageIsError(true);
    } finally {
      setIsUploadingScorm(false);
    }
  };

  const onLaunchScorm = async (packageId: string, attemptId?: string) => {
    setIsLaunchingScorm(packageId);
    setScormMessage("");
    setScormMessageIsError(false);
    try {
      let resolvedAttemptId = attemptId;
      if (!resolvedAttemptId) {
        const createResponse = await apiFetch(`${API_BASE_URL}/scorm/packages/${packageId}/attempts`, {
          method: "POST",
        });
        if (!createResponse.ok) {
          throw new Error(await readErrorMessage(createResponse));
        }
        const createdAttempt = (await createResponse.json()) as ScormAttemptSummary;
        resolvedAttemptId = createdAttempt.attempt_id;
      }
      const viewerUrl = `${API_BASE_URL}/scorm/packages/${packageId}/viewer?attempt_id=${encodeURIComponent(resolvedAttemptId ?? "")}&captions=true`;
      window.open(viewerUrl, "_blank", "noopener");
      await loadScormPackageDetail(packageId);
      await loadScormPackages();
      setScormMessage(`Opened viewer for ${packageId}.`);
      setScormMessageIsError(false);
    } catch (error) {
      setScormMessage(error instanceof Error ? error.message : "Failed to launch SCORM viewer.");
      setScormMessageIsError(true);
    } finally {
      setIsLaunchingScorm(null);
    }
  };

  const hasUnsavedDraft = hasUnsavedSegmentChanges;

  const packageOptions: Array<{ format: PackageFormat; label: string; description: string }> = [
    { format: "scorm12", label: "SCORM 1.2", description: "Legacy LMS package" },
    { format: "scorm2004", label: "SCORM 2004", description: "Modern SCORM package" },
    { format: "aicc", label: "AICC", description: "Older compliance package" },
    { format: "xapi", label: "xAPI", description: "Tin Can style launch package" },
    { format: "cmi5", label: "cmi5", description: "xAPI-based LMS package" },
  ];

  const stepStatus = (step: WorkflowStepKey): "current" | "done" | "pending" => {
    const order = WORKFLOW_STEPS.map((item) => item.key);
    const currentIndex = order.indexOf(activeStep);
    const stepIndex = order.indexOf(step);
    if (step === activeStep) {
      return "current";
    }
    if (stepIndex < currentIndex) {
      return "done";
    }
    return "pending";
  };

  const activeWorkspaceSection = useMemo(
    () => WORKSPACE_SECTIONS.find((section) => section.steps.includes(activeStep)) ?? WORKSPACE_SECTIONS[0],
    [activeStep],
  );

  const activeProcessingJob = useMemo(() => {
    if (selectedJob && selectedJob.stage !== "ready") {
      return selectedJob;
    }
    return jobs.find((job) => job.stage !== "ready") ?? null;
  }, [jobs, selectedJob]);

  const artifactGroups = useMemo(() => {
    const artifacts = selectedJobDetail?.artifacts ?? [];
    return {
      subtitle: artifacts.filter((artifact) => artifact.artifact_kind.startsWith("transcript-")),
      media: artifacts.filter((artifact) => artifact.artifact_kind.startsWith("video-")),
      package: artifacts.filter((artifact) => artifact.artifact_kind.startsWith("package-")),
    };
  }, [selectedJobDetail?.artifacts]);

  const onBuildPackage = async (format: PackageFormat) => {
    if (!editorJob) {
      return;
    }
    const requestedBaseName = exportBaseName.trim();
    setExportingFormat(format);
    setExportMessage("");
    setExportMessageIsError(false);
    try {
      await ensureTranscriptSavedForExport();
      const response = await apiFetch(`${API_BASE_URL}/jobs/${editorJob.job_id}/artifacts/build/package/${format}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          output_filename: requestedBaseName || null,
        }),
      });
      if (!response.ok) {
        throw new Error(await readErrorMessage(response));
      }
      const artifact = (await response.json()) as GeneratedArtifact;
      await refreshSelectedJobDetail();
      await onDownloadArtifact(artifact);
    } catch (error) {
      setExportMessage(
        error instanceof Error ? error.message : `Failed to build ${format} package.`,
      );
      setExportMessageIsError(true);
    } finally {
      setExportingFormat(null);
    }
  };

  const onExportSoftsubMp4 = async () => {
    if (!editorJob) {
      return;
    }
    const outputDir = softsubOutputDir.trim();
    const outputName = softsubOutputName.trim();
    if (!outputName) {
      setExportMessageIsError(true);
      setExportMessage("Choose an output file name.");
      return;
    }
    const shouldDownloadToBrowser = softsubExportMode === "browser";
    if (!shouldDownloadToBrowser && !outputDir) {
      setExportMessageIsError(true);
      setExportMessage("Choose a host machine output directory.");
      return;
    }

    const separator = outputDir.endsWith("/") || outputDir.endsWith("\\") ? "" : "/";
    const outputPath = outputDir ? `${outputDir}${separator}${outputName}` : outputName;
    setExportingFormat("mp4-softsub");
    setExportMessage("");
    setExportMessageIsError(false);
    try {
      await ensureTranscriptSavedForExport();
      const response = await apiFetch(`${API_BASE_URL}/jobs/${editorJob.job_id}/export/mp4-softsub`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          output_path: shouldDownloadToBrowser ? null : outputPath,
          output_filename: outputName,
          download: false,
          track_ids: selectedJobDetail?.subtitle_tracks
            ?.filter((track) => track.is_active)
            .map((track) => track.track_id) ?? [],
        }),
      });
      if (!response.ok) {
        throw new Error(await readErrorMessage(response));
      }
      const data = (await response.json()) as SoftSubtitleExportResponse;
      if (shouldDownloadToBrowser) {
        const artifactId = data.artifact_id;
        if (!artifactId) {
          throw new Error("Softsub export completed but no downloadable artifact was returned.");
        }
        await downloadFromApi(`${API_BASE_URL}/jobs/${editorJob.job_id}/artifacts/${artifactId}/download`, outputName);
        setExportMessage(`Downloaded ${outputName}.`);
      } else {
        setExportMessage(data.message);
      }
      setExportMessageIsError(false);
      await refreshSelectedJobDetail();
    } catch (error) {
      setExportMessage(
        error instanceof Error
          ? error.message
          : "Failed to export MP4 with soft subtitles.",
      );
      setExportMessageIsError(true);
    } finally {
      setExportingFormat(null);
    }
  };

  return (
    <div className="app-shell">
      <header className="topbar">
        <h1>Subtitle Workstation</h1>
        <p>Create subtitles, export deliverables, and package content for LMS playback.</p>
        <p>
          Backend status: <strong>{backendStatus}</strong>
        </p>
      </header>

      <main className="workflow-shell">
        <aside className="workflow-sidebar">
          <Panel title="Workspace">
            <div className="workspace-sections">
              {WORKSPACE_SECTIONS.map((section) => {
                const isActive = section.steps.includes(activeStep);
                return (
                  <button
                    key={section.label}
                    type="button"
                    className={`workspace-section-chip ${isActive ? "active" : ""}`}
                    onClick={() => setActiveStep(section.steps[0])}
                  >
                    <span className="workspace-section-label">{section.label}</span>
                    <span className="workspace-section-desc">{section.description}</span>
                  </button>
                );
              })}
            </div>
            <div className="workflow-steps compact">
              {activeWorkspaceSection.steps
                .filter((step) => step !== activeStep)
                .map((step) => {
                  const stepMeta = WORKFLOW_STEPS.find((item) => item.key === step);
                  if (!stepMeta) return null;
                  return (
                    <button
                      key={step}
                      type="button"
                      className={`workflow-step workflow-step-${stepStatus(step)}`}
                      onClick={() => setActiveStep(step)}
                    >
                      <div>
                        <div className="workflow-step-label">{stepMeta.label}</div>
                        <div className="workflow-step-desc">{stepMeta.description}</div>
                      </div>
                    </button>
                  );
                })}
            </div>
            <div className="workflow-summary">
              <p><strong>Project:</strong> {editorJob ? summarizePath(editorJob.media_path) : "No project loaded"}</p>
              <p><strong>Status:</strong> {selectedJobDetail?.stage ?? selectedJob?.stage ?? "Idle"}</p>
              <p><strong>Draft:</strong> {hasUnsavedDraft ? "Unsaved changes" : "Saved"}</p>
              {editorJob ? (
                <div className="ingest-progress-card">
                  <p className="subtitle-panel-title">Current progress</p>
                  <div className="progress-track" aria-hidden="true">
                    <div className="progress-fill" style={{ width: `${editorJob.progress_percent}%` }} />
                  </div>
                  <p className="muted">{editorJob.progress_percent}% · {editorJob.stage}</p>
                </div>
              ) : null}
              <button type="button" className="secondary-btn" onClick={() => setActiveStep("scorm-lms")}>Open Player Tools</button>
            </div>
          </Panel>
        </aside>
        <section className="workflow-main">
          {activeProcessingJob ? (
            <div className={`processing-status-card ${activeProcessingJob.stage === "ready" ? "done" : "active"}`}>
              <div className="processing-status-header">
                <div>
                  <p className="processing-status-kicker">Live progress</p>
                  <h3>{summarizePath(activeProcessingJob.media_path)}</h3>
                </div>
                <div className="processing-status-percent">{activeProcessingJob.progress_percent}%</div>
              </div>
              <div className="processing-status-track" aria-hidden="true">
                <div className="processing-status-fill" style={{ width: `${activeProcessingJob.progress_percent}%` }} />
              </div>
              <p className="processing-status-text">
                {activeProcessingJob.stage === "queued"
                  ? "Queued and preparing transcription…"
                  : activeProcessingJob.stage === "transcribing"
                    ? "Transcribing audio now…"
                    : activeProcessingJob.stage === "aligned"
                      ? "Aligning subtitles to timing…"
                      : activeProcessingJob.stage === "diarized"
                        ? "Finalizing speaker and subtitle structure…"
                        : `Stage: ${activeProcessingJob.stage}`}
              </p>
            </div>
          ) : null}
          {(activeStep === "load") ? (
            <>
          <Panel title="Bring in source media">
            <p>Choose the revised MP4 and, optionally, the legacy VTT/SRT whose text corrections should carry forward.</p>
            <form className="ingest-form" onSubmit={onIngest}>
              <div className="source-upload-grid">
                <div className="source-upload-card">
                  <div className="source-upload-header">
                    <p className="subtitle-panel-title">MP4 source</p>
                    <button
                      type="button"
                      className="secondary-btn source-upload-button"
                      onClick={() => mediaFileInputRef.current?.click()}
                    >
                      Upload MP4
                    </button>
                  </div>
                  <label className="source-path-field">
                    <span>MP4 path</span>
                    <input
                      className="media-input"
                      type="text"
                      value={mediaPath}
                      placeholder="/path/to/revised-video.mp4"
                      onChange={(event) => {
                        setMediaPath(event.target.value);
                        setSelectedFile(null);
                        setSelectedFileName("");
                        setIngestMessage("");
                        setIngestMessageIsError(false);
                      }}
                    />
                  </label>
                  <input
                    ref={mediaFileInputRef}
                    className="hidden-file-input"
                    type="file"
                    accept="video/*,.mp4,.mov,.m4v,.webm,.mkv"
                    onChange={onSelectLocalFile}
                  />
                  {selectedFileName ? <p className="selected-asset-name">Selected: {selectedFileName}</p> : null}
                </div>

                <div className="source-upload-card optional">
                  <div className="source-upload-header">
                    <p className="subtitle-panel-title">Legacy subtitle</p>
                    <button
                      type="button"
                      className="secondary-btn source-upload-button"
                      onClick={() => legacySubtitleInputRef.current?.click()}
                    >
                      Upload Old VTT
                    </button>
                  </div>
                  <label className="source-path-field">
                    <span>VTT/SRT path</span>
                    <input
                      className="media-input"
                      type="text"
                      value={legacySubtitlePath}
                      placeholder="/path/to/legacy-edited.vtt"
                      onChange={(event) => {
                        setLegacySubtitlePath(event.target.value);
                        setLegacySubtitleFile(null);
                        setLegacySubtitleFileName("");
                        setIngestMessage("");
                        setIngestMessageIsError(false);
                      }}
                    />
                  </label>
                  <input
                    ref={legacySubtitleInputRef}
                    className="hidden-file-input"
                    type="file"
                    accept=".srt,.vtt,text/vtt,application/x-subrip"
                    onChange={onSelectLegacySubtitleFile}
                  />
                  {legacySubtitleFileName ? <p className="selected-asset-name">Selected: {legacySubtitleFileName}</p> : null}
                </div>
              </div>
              <button className="start-project-button" type="submit" disabled={!canSubmitIngest || isSubmittingIngest}>
                {isSubmittingIngest ? "Uploading..." : "Start Project"}
              </button>
            </form>
            {selectedFileName || legacySubtitleFileName ? (
              <div className="asset-summary-card">
                <p className="subtitle-panel-title">Ready to import</p>
                {selectedFileName ? <p className="muted">Video: {selectedFileName}</p> : null}
                {legacySubtitleFileName ? <p className="muted">Legacy subtitles: {legacySubtitleFileName}</p> : null}
              </div>
            ) : null}
            {isSubmittingIngest ? (
              <div className="ingest-progress-card">
                <p className="subtitle-panel-title">Loading video</p>
                <div className="progress-track" aria-hidden="true">
                  <div className="progress-fill" style={{ width: "65%" }} />
                </div>
                <p className="muted">Uploading assets and preparing the job...</p>
              </div>
            ) : null}
            {ingestMessage ? (
              <p className={`status-message ${ingestMessageIsError ? "error" : ""}`}>
                {ingestMessage}
              </p>
            ) : null}
          </Panel>

            </>
          ) : null}

          {(activeStep === "edit" || activeStep === "commit") ? (
            <>
          <Panel title={activeStep === "commit" ? "Review & Save" : "Edit Subtitles"}>
            <div className="subtitle-panel">
              <p className="muted">Subtitle timeline preview</p>
              <div className="waveform-strip" aria-hidden="true">
                <div className="waveform-fill" />
              </div>
              {isLoadingJobDetail ? <p className="muted">Loading job detail...</p> : null}
              {jobDetailError ? <p className="status-message error">{jobDetailError}</p> : null}
              {editorJob ? (
                <>
                <div className="asset-summary-card">
                  <p className="subtitle-panel-title">Current video</p>
                  <p><strong>{editorJob.media_metadata?.file_name ?? summarizePath(editorJob.media_path)}</strong></p>
                  <p className="muted">Stage: {editorJob.stage} · {transcriptSegments.length} subtitle segments</p>
                  <div className="progress-track" aria-hidden="true">
                    <div className="progress-fill" style={{ width: `${editorJob.progress_percent}%` }} />
                  </div>
                </div>
                <details className="technical-details">
                  <summary>Technical details</summary>
                  <dl className="selected-job-meta">
                  <div>
                    <dt>Job ID</dt>
                    <dd>{editorJob.job_id}</dd>
                  </div>
                  <div>
                    <dt>Stage</dt>
                    <dd>
                      {editorJob.stage} ({editorJob.progress_percent}%)
                    </dd>
                  </div>
                  <div>
                    <dt>Media Path</dt>
                    <dd>{editorJob.media_path}</dd>
                  </div>
                  <div>
                    <dt>Transcript Mode</dt>
                    <dd>{editorJob.transcription_mode}</dd>
                  </div>
                  <div>
                    <dt>Transcript Source</dt>
                    <dd>{editorJob.transcription_source}</dd>
                  </div>
                  {editorJob.media_metadata ? (
                    <>
                      <div>
                        <dt>File Name</dt>
                        <dd>{editorJob.media_metadata.file_name}</dd>
                      </div>
                      <div>
                        <dt>File Size</dt>
                        <dd>{formatBytes(editorJob.media_metadata.size_bytes)}</dd>
                      </div>
                      <div>
                        <dt>Duration</dt>
                        <dd>{formatDuration(editorJob.media_metadata.duration_seconds)}</dd>
                      </div>
                      <div>
                        <dt>Streams</dt>
                        <dd>
                          audio:{" "}
                          {editorJob.media_metadata.has_audio === null
                            ? "unknown"
                            : editorJob.media_metadata.has_audio
                              ? "yes"
                              : "no"}
                          , video:{" "}
                          {editorJob.media_metadata.has_video === null
                            ? "unknown"
                            : editorJob.media_metadata.has_video
                              ? "yes"
                              : "no"}
                        </dd>
                      </div>
                    </>
                  ) : null}
                  <div>
                    <dt>Updated</dt>
                    <dd>{formatTimestamp(editorJob.updated_at)}</dd>
                  </div>
                </dl>
                </details>
                </>
              ) : (
                <p className="muted">No loaded video selected yet. Start in Load Assets, then return here to edit subtitles.</p>
              )}
              {editorJob ? (
                <div className="subtitle-panel">
                  <div className="subtitle-panel-header">
                    <div>
                      <p className="subtitle-panel-title">Subtitle master</p>
                      <p className="muted">Review the current timing, text, and speaker labels.</p>
                    </div>
                  </div>
                  {retimeReport ? (
                    <div className="retime-report-card">
                      <div>
                        <p className="subtitle-panel-title">Subtitle text applied and adjusted</p>
                        <p className="muted">
                          {retimeReport.source_file_name} mapped onto the current timing at {(retimeReport.average_confidence * 100).toFixed(0)}% average confidence.
                        </p>
                      </div>
                      <div className="retime-stat-grid">
                        <span><strong>{retimeReport.matched_segments}</strong> matched</span>
                        <span><strong>{retimeReport.low_confidence_segments}</strong> review</span>
                        <span><strong>{retimeReport.unmatched_new_segments}</strong> new</span>
                        <span><strong>{retimeReport.unmatched_old_segments}</strong> old-only</span>
                        <span><strong>{retimeReport.learned_corrections.length}</strong> learned fixes</span>
                        <span><strong>{retimeReport.applied_corrections}</strong> auto-fixed</span>
                        <span><strong>{retimeReport.sore_thumb_segments}</strong> sore thumbs</span>
                      </div>
                      <div className="retime-report-actions">
                        <button
                          type="button"
                          className="secondary-btn"
                          onClick={() => setShowLowConfidenceOnly((current) => !current)}
                          disabled={retimeReport.low_confidence_segments === 0}
                        >
                          {showLowConfidenceOnly ? "Show All Segments" : "Review Low Confidence"}
                        </button>
                      </div>
                    </div>
                  ) : null}
                  {activeStep === "commit" ? (
                    <div className="commit-banner">
                      <p><strong>{hasUnsavedDraft ? "Subtitle changes are ready to save." : "All subtitle changes are saved."}</strong></p>
                      <p className="muted">Save Changes commits text, speaker, and timing edits before export or packaging.</p>
                    </div>
                  ) : null}
                  {segmentSaveMessage ? (
                    <p className={`status-message ${segmentSaveMessageIsError ? "error" : ""}`}>
                      {segmentSaveMessage}
                    </p>
                  ) : null}
                  {transcriptSegments.length === 0 ? (
                    <div className="asset-summary-card">
                      <p className="subtitle-panel-title">No subtitle content loaded yet</p>
                      <p className="muted">This video does not currently have timing segments. Generate timing first, then apply subtitle text against that timing.</p>
                    </div>
                  ) : (
                    <>
                      <div className="asset-summary-card">
                        <p className="subtitle-panel-title">Loaded subtitle content</p>
                        <p className="muted">
                          Showing {visibleTranscriptSegments.length} of {transcriptSegments.length} subtitle segments for the selected video.
                        </p>
                        <p>{transcriptSegments[0]?.text ?? ""}</p>
                        <p className="muted">Use the list below to adjust timing, text, and speaker labels.</p>
                      </div>
                      <div className="segment-list-header">
                        <div>
                          <p className="subtitle-panel-title">Segments</p>
                          <p className="muted">Fine-tune timing and subtitle text one segment at a time.</p>
                        </div>
                        <button
                          type="button"
                          className="secondary-btn"
                          onClick={() => void onSaveSegments()}
                          disabled={
                            !selectedJobDetail ||
                            isLoadingJobDetail ||
                            isSavingSegments ||
                            isRetimingSubtitles ||
                            isImportingEmbeddedSubtitles ||
                            !hasUnsavedSegmentChanges
                          }
                        >
                          {isSavingSegments ? "Saving..." : "Save Changes"}
                        </button>
                      </div>
                      <ul className="subtitle-list">
                        {visibleTranscriptSegments.map((segment) => {
                          const index = transcriptSegments.findIndex(
                            (candidate) => candidate.segment_id === segment.segment_id,
                          );
                          if (index < 0) {
                            return null;
                          }
                          const duration = segment.end_seconds - segment.start_seconds;
                          const isExpanded = expandedSegmentId === segment.segment_id;
                          return (
                            <li className={`subtitle-item ${isExpanded ? "expanded" : "collapsed"}`} key={segment.segment_id}>
                              <button
                                type="button"
                                className="subtitle-summary-btn"
                                onClick={() =>
                                  setExpandedSegmentId((current) =>
                                    current === segment.segment_id ? null : segment.segment_id,
                                  )
                                }
                              >
                                <div className="subtitle-item-header">
                                  <div>
                                    <div className="subtitle-segment-index">Segment {index + 1}</div>
                                    <div className="subtitle-summary-text">{segment.text || "Untitled subtitle segment"}</div>
                                    {segment.retime_status ? (
                                      <div className="retime-badge-row">
                                        <span className={`retime-badge ${segment.retime_status}`}>
                                          {segment.retime_status === "low-confidence"
                                            ? "Review"
                                            : segment.retime_status === "new-only"
                                              ? "New timing only"
                                              : segment.retime_status === "corrected"
                                                ? "Auto-corrected"
                                                : segment.retime_status === "sore-thumb"
                                                  ? "Sore thumb"
                                                  : "Matched"}
                                        </span>
                                        {typeof segment.retime_confidence === "number" ? (
                                          <span className="retime-confidence">
                                            {(segment.retime_confidence * 100).toFixed(0)}%
                                          </span>
                                        ) : null}
                                      </div>
                                    ) : null}
                                  </div>
                                  <div className="subtitle-time">
                                    {formatTimecode(segment.start_seconds)} - {formatTimecode(segment.end_seconds)}
                                    <span className="subtitle-duration">{duration.toFixed(2)}s</span>
                                  </div>
                                </div>
                              </button>
                              {isExpanded ? (
                                <>
                                  <div className="subtitle-row">
                                    <label className="subtitle-edit-field">
                                      <span>Start</span>
                                      <input
                                        type="number"
                                        step="0.01"
                                        min="0"
                                        value={segment.start_seconds}
                                        onChange={(event) =>
                                          onSegmentNumberChange(
                                            index,
                                            "start_seconds",
                                            event.target.value,
                                          )
                                        }
                                        disabled={!selectedJobDetail || isSavingSegments}
                                      />
                                    </label>
                                    <label className="subtitle-edit-field">
                                      <span>End</span>
                                      <input
                                        type="number"
                                        step="0.01"
                                        min="0"
                                        value={segment.end_seconds}
                                        onChange={(event) =>
                                          onSegmentNumberChange(
                                            index,
                                            "end_seconds",
                                            event.target.value,
                                          )
                                        }
                                        disabled={!selectedJobDetail || isSavingSegments}
                                      />
                                    </label>
                                    <div className="subtitle-time subtitle-time-secondary">
                                      Adjust timing precisely below
                                    </div>
                                  </div>
                                  <label className="subtitle-edit-field subtitle-text-field">
                                    <span>Text</span>
                                    {segment.retime_note ? (
                                      <p className="retime-note">{segment.retime_note}</p>
                                    ) : null}
                                    {segment.correction_suggestions?.length ? (
                                      <div className="retime-note">
                                        {segment.correction_suggestions.map((correction, correctionIndex) => (
                                          <p key={`${segment.segment_id}-correction-${correctionIndex}`}>
                                            {correction.status === "applied" ? "Applied" : "Check"}: "{correction.wrong_text}" {"->"} "{correction.corrected_text}" ({(correction.confidence * 100).toFixed(0)}%)
                                          </p>
                                        ))}
                                      </div>
                                    ) : null}
                                    <textarea
                                      value={segment.text}
                                      onChange={(event) => onSegmentTextChange(index, event.target.value)}
                                      rows={3}
                                      disabled={!selectedJobDetail || isSavingSegments}
                                    />
                                  </label>
                                  <label className="subtitle-edit-field subtitle-speaker-edit">
                                    <span>Speaker</span>
                                    <input
                                      type="text"
                                      value={segment.speaker ?? ""}
                                      onChange={(event) =>
                                        onSegmentSpeakerChange(index, event.target.value)
                                      }
                                      placeholder="Optional"
                                      disabled={!selectedJobDetail || isSavingSegments}
                                    />
                                  </label>
                                </>
                              ) : null}
                            </li>
                          );
                        })}
                      </ul>
                    </>
                  )}
                </div>
              ) : null}
            </div>
            {/* TODO: Integrate waveform renderer and segment editing interactions. */}
            {/* TODO: Overlay speaker segments from Pyannote diarization. */}
          </Panel>
            </>
          ) : null}

          {(activeStep === "outputs" || activeStep === "packages") ? (
            <>
          <Panel title={activeStep === "packages" ? "Build Packages" : "Deliver Exports"}>
            <p>
              {activeStep === "packages"
                ? "Create LMS-ready packages from the current video and subtitle master."
                : "Export subtitle files and MP4 deliverables for handoff or review."}
            </p>
            {activeStep === "packages" ? (
              <div className="subtitle-panel-actions" style={{ marginBottom: "0.75rem" }}>
                <button type="button" className="secondary-btn" onClick={() => setActiveStep("scorm-lms")}>
                  Open Player Tools
                </button>
              </div>
            ) : null}
            <div className="deliverable-actions-card" style={{ marginBottom: "1rem" }}>
              <label className="subtitle-edit-field">
                <span>Export / Package Base Name</span>
                <input
                  type="text"
                  value={exportBaseName}
                  onChange={(event) => setExportBaseName(event.target.value)}
                  placeholder="Master 09"
                  disabled={!editorJob || exportingFormat !== null}
                />
              </label>
              <p className="muted">Used for SRT, VTT, and package filenames so deliveries stay consistent and human-readable.</p>
            </div>
            {activeStep === "outputs" ? (
            <>
            <div className="deliverable-actions-card">
            <div className="export-actions">
              <button
                type="button"
                onClick={() => void onExport("srt")}
                disabled={!editorJob || exportingFormat !== null}
              >
                {exportingFormat === "srt" ? "Exporting..." : "Export SRT"}
              </button>
              <button
                type="button"
                onClick={() => void onExport("vtt")}
                disabled={!editorJob || exportingFormat !== null}
              >
                {exportingFormat === "vtt" ? "Exporting..." : "Export VTT"}
              </button>
            </div>
            </div>
            <div className="softsub-export-box">
              <p className="subtitle-panel-title">MP4 with Soft Subtitles</p>
              <label className="subtitle-edit-field">
                <span>Primary Subtitle Language</span>
                <input
                  type="text"
                  value={softsubLanguage}
                  onChange={(event) => setSoftsubLanguage(event.target.value)}
                  placeholder="eng"
                  disabled={!editorJob || exportingFormat !== null}
                />
              </label>
              <label className="subtitle-edit-field">
                <span>Primary Subtitle Label</span>
                <input
                  type="text"
                  value={softsubLabel}
                  onChange={(event) => setSoftsubLabel(event.target.value)}
                  placeholder="English Subtitles"
                  disabled={!editorJob || exportingFormat !== null}
                />
              </label>
              <div className="subtitle-panel-header">
                <p className="subtitle-panel-title">Stored Subtitle Tracks</p>
              </div>
              {selectedJobDetail?.subtitle_tracks?.length ? (
                <ul className="subtitle-list">
                  {selectedJobDetail.subtitle_tracks.map((track) => (
                    <li className="subtitle-item" key={track.track_id}>
                      <div><strong>{track.label}</strong> ({track.language})</div>
                      <div className="muted">{track.track_id} · {track.source_kind} · {track.format_name}</div>
                      <div className="muted">{track.subtitle_path ?? "Uses current edited transcript"}</div>
                      <div className="muted">{track.is_default ? "Default track" : "Not default"}</div>
                      {track.source_kind !== "edited-transcript" ? (
                        <button
                          type="button"
                          className="secondary-btn"
                          onClick={() => void onDeleteStoredTrack(track.track_id)}
                          disabled={isCreatingTrack || exportingFormat !== null}
                        >
                          Remove Track
                        </button>
                      ) : null}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="muted">No stored subtitle tracks yet.</p>
              )}
              <div className="subtitle-panel-header">
                <p className="subtitle-panel-title">Additional Embedded Tracks</p>
                <button
                  type="button"
                  className="secondary-btn"
                  onClick={onAddAdditionalTrack}
                  disabled={!editorJob || exportingFormat !== null}
                >
                  Add Track
                </button>
              </div>
              {additionalTrackDrafts.length === 0 ? (
                <p className="muted">No extra tracks yet. Add .srt or .vtt subtitle files for other languages.</p>
              ) : (
                <ul className="subtitle-list">
                  {additionalTrackDrafts.map((track, index) => (
                    <li className="subtitle-item" key={`additional-track-${index}`}>
                      <label className="subtitle-edit-field">
                        <span>Language</span>
                        <input
                          type="text"
                          value={track.language}
                          onChange={(event) =>
                            onAdditionalTrackChange(index, "language", event.target.value)
                          }
                          placeholder="spa"
                          disabled={!editorJob || exportingFormat !== null}
                        />
                      </label>
                      <label className="subtitle-edit-field">
                        <span>Label</span>
                        <input
                          type="text"
                          value={track.label}
                          onChange={(event) =>
                            onAdditionalTrackChange(index, "label", event.target.value)
                          }
                          placeholder="Spanish Subtitles"
                          disabled={!editorJob || exportingFormat !== null}
                        />
                      </label>
                      <label className="subtitle-edit-field">
                        <span>Subtitle File Path (.srt/.vtt)</span>
                        <input
                          type="text"
                          value={track.subtitle_path}
                          onChange={(event) =>
                            onAdditionalTrackChange(index, "subtitle_path", event.target.value)
                          }
                          placeholder="/Users/kuon/Desktop/spanish.srt"
                          disabled={!editorJob || exportingFormat !== null}
                        />
                      </label>
                      <label className="subtitle-edit-field">
                        <span>Default Track</span>
                        <input
                          type="checkbox"
                          checked={track.is_default}
                          onChange={(event) =>
                            onAdditionalTrackChange(index, "is_default", event.target.checked)
                          }
                          disabled={!editorJob || exportingFormat !== null}
                        />
                      </label>
                      <div className="subtitle-panel-header">
                        <button
                          type="button"
                          className="secondary-btn"
                          onClick={() => void onCreateStoredTrack(track)}
                          disabled={!editorJob || exportingFormat !== null || isCreatingTrack || !track.subtitle_path.trim()}
                        >
                          {isCreatingTrack ? "Saving..." : "Save as Track"}
                        </button>
                        <button
                          type="button"
                          className="secondary-btn"
                          onClick={() => onRemoveAdditionalTrack(index)}
                          disabled={!editorJob || exportingFormat !== null || isCreatingTrack}
                        >
                          Remove Draft
                        </button>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
              <label className="subtitle-edit-field">
                <span>Delivery</span>
                <select
                  value={softsubExportMode}
                  onChange={(event) => setSoftsubExportMode(event.target.value as SoftsubExportMode)}
                  disabled={!editorJob || exportingFormat !== null}
                >
                  <option value="browser">Download in browser (recommended)</option>
                  <option value="host">Save on host machine</option>
                </select>
              </label>
              {softsubExportMode === "host" ? (
                <label className="subtitle-edit-field">
                  <span>Host Output Directory</span>
                  <input
                    type="text"
                    value={softsubOutputDir}
                    onChange={(event) => setSoftsubOutputDir(event.target.value)}
                    placeholder="/Users/kuon/Desktop"
                    disabled={!editorJob || exportingFormat !== null}
                  />
                </label>
              ) : (
                <p className="muted">The MP4 will download to the local browser client's configured Downloads folder.</p>
              )}
              <label className="subtitle-edit-field">
                <span>Output File Name</span>
                <input
                  type="text"
                  value={softsubOutputName}
                  onChange={(event) => setSoftsubOutputName(event.target.value)}
                  placeholder="Master 09.softsubs.mp4"
                  disabled={!editorJob || exportingFormat !== null}
                />
              </label>
              <button
                type="button"
                onClick={() => void onExportSoftsubMp4()}
                disabled={!editorJob || exportingFormat !== null}
              >
                {exportingFormat === "mp4-softsub"
                  ? "Exporting..."
                  : "Export MP4 (Soft Subtitles)"}
              </button>
            </div>
            </>
            ) : null}

            {activeStep === "packages" ? (
              <div className="deliverable-actions-card">
              <div className="package-grid">
                {packageOptions.map((option) => (
                  <button
                    key={option.format}
                    type="button"
                    className="package-card"
                    onClick={() => void onBuildPackage(option.format)}
                    disabled={!editorJob || exportingFormat !== null}
                  >
                    <strong>{exportingFormat === option.format ? "Building..." : option.label}</strong>
                    <span>{option.description}</span>
                  </button>
                ))}
              </div>
              </div>
            ) : null}

            {selectedJobDetail?.artifacts?.length ? (
              <>
              <div className="deliverables-header">
                <div>
                  <p className="subtitle-panel-title">Recent deliverables</p>
                  <p className="muted">Download the latest subtitle files, media exports, and package builds.</p>
                </div>
              </div>
              <div className="artifact-groups">
                <div className="artifact-group-card">
                  <p className="subtitle-panel-title">Subtitle files</p>
                  {artifactGroups.subtitle.length > 0 ? (
                    <ul className="artifact-list">
                      {artifactGroups.subtitle.map((artifact) => (
                        <li className="artifact-item" key={artifact.artifact_id}>
                          <div>
                            <strong>{artifact.file_name}</strong>
                            <div className="muted">
                              {artifact.format_name} · {formatBytes(artifact.size_bytes)} · {formatRelativeTime(artifact.created_at)}
                            </div>
                          </div>
                          <button
                            type="button"
                            className="secondary-btn"
                            onClick={() => void onDownloadArtifact(artifact)}
                            disabled={artifactActionKey !== null || exportingFormat !== null}
                          >
                            {artifactActionKey === `download-${artifact.artifact_id}` ? "Downloading..." : "Download"}
                          </button>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="muted">No subtitle files generated yet.</p>
                  )}
                </div>
                <div className="artifact-group-card">
                  <p className="subtitle-panel-title">Media exports</p>
                  {artifactGroups.media.length > 0 ? (
                    <ul className="artifact-list">
                      {artifactGroups.media.map((artifact) => (
                        <li className="artifact-item" key={artifact.artifact_id}>
                          <div>
                            <strong>{artifact.file_name}</strong>
                            <div className="muted">
                              {artifact.format_name} · {formatBytes(artifact.size_bytes)} · {formatRelativeTime(artifact.created_at)}
                            </div>
                          </div>
                          <button
                            type="button"
                            className="secondary-btn"
                            onClick={() => void onDownloadArtifact(artifact)}
                            disabled={artifactActionKey !== null || exportingFormat !== null}
                          >
                            {artifactActionKey === `download-${artifact.artifact_id}` ? "Downloading..." : "Download"}
                          </button>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="muted">No media outputs generated yet.</p>
                  )}
                </div>
                <div className="artifact-group-card">
                  <p className="subtitle-panel-title">Packages</p>
                  {artifactGroups.package.length > 0 ? (
                    <ul className="artifact-list">
                      {artifactGroups.package.map((artifact) => (
                        <li className="artifact-item" key={artifact.artifact_id}>
                          <div>
                            <strong>{artifact.file_name}</strong>
                            <div className="muted">
                              {artifact.format_name} · {formatBytes(artifact.size_bytes)} · {formatRelativeTime(artifact.created_at)}
                            </div>
                          </div>
                          <button
                            type="button"
                            className="secondary-btn"
                            onClick={() => void onDownloadArtifact(artifact)}
                            disabled={artifactActionKey !== null || exportingFormat !== null}
                          >
                            {artifactActionKey === `download-${artifact.artifact_id}` ? "Downloading..." : "Download"}
                          </button>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="muted">No package files generated yet.</p>
                  )}
                </div>
              </div>
              </>
            ) : null}
            {exportMessage ? (
              <p className={`status-message ${exportMessageIsError ? "error" : ""}`}>
                {exportMessage}
              </p>
            ) : null}
          </Panel>
            </>
          ) : null}

          {activeStep === "scorm-lms" ? (
            <>
              <Panel title="SCORM Player">
                <p>Upload a SCORM 1.2 or SCORM 2004 zip, review launch readiness, then open a fresh player session or resume a saved attempt.</p>
                <form className="ingest-form" onSubmit={onUploadScorm}>
                  <label className="secondary-btn file-action-btn">
                    Choose SCORM ZIP
                    <input
                      type="file"
                      accept=".zip,application/zip,application/x-zip-compressed"
                      onChange={onSelectScormFile}
                    />
                  </label>
                  <button type="submit" disabled={!scormFile || isUploadingScorm}>
                    {isUploadingScorm ? "Uploading..." : "Upload & Validate"}
                  </button>
                </form>
                {scormFileName ? <p className="muted">Selected: {scormFileName}</p> : null}
                {scormMessage ? (
                  <p className={`status-message ${scormMessageIsError ? "error" : ""}`}>{scormMessage}</p>
                ) : null}
              </Panel>

              <Panel title="SCORM Packages">
                {scormPackages.length === 0 ? (
                  <p className="muted">No SCORM packages uploaded yet.</p>
                ) : (
                  <div className="job-list">
                    {scormPackages.map((pkg) => (
                      <button
                        key={pkg.package_id}
                        type="button"
                        className={`job-list-item ${selectedScormPackageId === pkg.package_id ? "selected" : ""}`}
                        onClick={() => setSelectedScormPackageId(pkg.package_id)}
                      >
                        <div>
                          <strong>{pkg.title}</strong>
                          <div className="muted">{pkg.file_name} · {pkg.scorm_version ?? "unknown version"}</div>
                        </div>
                        <div className="muted">{pkg.valid ? "ready" : "needs review"} · {pkg.issue_count}</div>
                      </button>
                    ))}
                  </div>
                )}
              </Panel>

              <Panel title="Launch Readiness">
                {!selectedScormPackage ? (
                  <p className="muted">Choose a package to review validation status and saved attempts.</p>
                ) : (
                  <div className="subtitle-panel">
                    <div className="asset-summary-card">
                      <p><strong>{selectedScormPackage.title}</strong></p>
                      <p className="muted">{selectedScormPackage.file_name}</p>
                      <p className="muted">Version: {selectedScormPackage.scorm_version ?? "unknown"} · Launch: {selectedScormPackage.launch_path ?? "n/a"}</p>
                      <p className="muted">Validation: {selectedScormPackage.valid ? "ready" : "needs review"}</p>
                    </div>

                    <div>
                      <p className="subtitle-panel-title">Validation</p>
                      {selectedScormPackage.issues.length === 0 ? (
                        <p className="muted">Ready to launch. No validation issues detected.</p>
                      ) : (
                        <ul className="artifact-list">
                          {selectedScormPackage.issues.map((issue, index) => (
                            <li key={`${issue.level}-${index}`} className={`status-message ${issue.level === "error" ? "error" : ""}`}>
                              <strong>{issue.level.toUpperCase()}</strong>: {issue.message}
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>

                    <div>
                      <p className="subtitle-panel-title">Saved attempts</p>
                      <div className="subtitle-panel-actions">
                        <button
                          type="button"
                          onClick={() => void onLaunchScorm(selectedScormPackage.package_id)}
                          disabled={!selectedScormPackage.valid || isLaunchingScorm === selectedScormPackage.package_id}
                        >
                          {isLaunchingScorm === selectedScormPackage.package_id ? "Opening..." : "Start New Session"}
                        </button>
                      </div>
                      {scormAttempts.length === 0 ? (
                        <p className="muted">No saved attempts yet.</p>
                      ) : (
                        <div className="artifact-list">
                          {scormAttempts.map((attempt) => (
                            <div key={attempt.attempt_id} className="artifact-item">
                              <div>
                                <strong>{attempt.completed ? "Completed session" : "In-progress session"}</strong>
                                <div className="muted">Updated {formatTimestamp(attempt.updated_at)}</div>
                                <div className="muted">Score: {attempt.score_raw ?? "—"} · Resume point: {attempt.location ?? "—"}</div>
                              </div>
                              <button
                                type="button"
                                className="secondary-btn"
                                onClick={() => void onLaunchScorm(selectedScormPackage.package_id, attempt.attempt_id)}
                              >
                                Resume Session
                              </button>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </Panel>
            </>
          ) : null}
        </section>
      </main>
    </div>
  );
}
