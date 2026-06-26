import { ChangeEvent, FormEvent, useEffect, useMemo, useState } from "react";

type PanelProps = {
  title: string;
  children: React.ReactNode;
};

const API_BASE_URL = "http://127.0.0.1:8000";

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

type TranscriptSegment = {
  segment_id: string;
  start_seconds: number;
  end_seconds: number;
  text: string;
  speaker: string | null;
};

type StoredSubtitleTrack = {
  track_id: string;
  source_kind: "edited-transcript" | "uploaded-subtitle" | "sidecar-subtitle" | "embedded-subtitle";
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

type JobDetail = JobSummary & {
  transcript_segments: TranscriptSegment[];
  transcript_is_edited: boolean;
  subtitle_tracks: StoredSubtitleTrack[];
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

export default function App() {
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
  const [isSavingSegments, setIsSavingSegments] = useState(false);
  const [segmentSaveMessage, setSegmentSaveMessage] = useState("");
  const [segmentSaveMessageIsError, setSegmentSaveMessageIsError] = useState(false);
  const [exportMessage, setExportMessage] = useState("");
  const [exportMessageIsError, setExportMessageIsError] = useState(false);
  const [exportingFormat, setExportingFormat] = useState<"srt" | "vtt" | "mp4-softsub" | null>(null);
  const [selectedFileName, setSelectedFileName] = useState("");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [softsubOutputDir, setSoftsubOutputDir] = useState("");
  const [softsubOutputName, setSoftsubOutputName] = useState("");
  const [subtitleImportFileName, setSubtitleImportFileName] = useState("");
  const [globalShiftSeconds, setGlobalShiftSeconds] = useState("5.0");
  const [isImportingSubtitles, setIsImportingSubtitles] = useState(false);
  const [softsubLanguage, setSoftsubLanguage] = useState("eng");
  const [softsubLabel, setSoftsubLabel] = useState("English Subtitles");
  const [additionalTrackDrafts, setAdditionalTrackDrafts] = useState<SubtitleTrackMetadataDraft[]>([]);
  const [subtitleStreams, setSubtitleStreams] = useState<SubtitleStream[]>([]);
  const [selectedSubtitleStreamIndex, setSelectedSubtitleStreamIndex] = useState("");
  const [isImportingEmbeddedSubtitles, setIsImportingEmbeddedSubtitles] = useState(false);
  const [sidecarSubtitles, setSidecarSubtitles] = useState<SidecarSubtitle[]>([]);
  const [isImportingSidecarSubtitles, setIsImportingSidecarSubtitles] = useState(false);
  const [isCreatingTrack, setIsCreatingTrack] = useState(false);

  const loadJobs = async () => {
    const response = await fetch(`${API_BASE_URL}/jobs`);
    if (!response.ok) {
      throw new Error(`jobs request failed (${response.status})`);
    }
    const data = (await response.json()) as JobSummary[];
    setJobs(data);
  };

  useEffect(() => {
    const loadData = async () => {
      try {
        const healthResponse = await fetch(`${API_BASE_URL}/health`);
        if (!healthResponse.ok) {
          throw new Error(`health request failed (${healthResponse.status})`);
        }
        const health = (await healthResponse.json()) as HealthResponse;
        setBackendStatus(`${health.status} (${health.service})`);
      } catch {
        setBackendStatus("offline");
      }

      try {
        await loadJobs();
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
      setSelectedJobId(jobs[0].job_id);
    }
  }, [jobs, selectedJobId]);

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
    const loadJobDetail = async () => {
      setIsLoadingJobDetail(true);
      setJobDetailError("");
      try {
        const response = await fetch(`${API_BASE_URL}/jobs/${selectedJobId}`);
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
        if (isActive) {
          setIsLoadingJobDetail(false);
        }
      }
    };

    void loadJobDetail();
    return () => {
      isActive = false;
    };
  }, [selectedJobId, selectedJob?.updated_at]);

  useEffect(() => {
    setSegmentDrafts(selectedJobDetail?.transcript_segments ?? []);
    setSegmentSaveMessage("");
    setSegmentSaveMessageIsError(false);
    setExportMessage("");
    setExportMessageIsError(false);
    setGlobalShiftSeconds("5.0");
  }, [selectedJobDetail?.job_id, selectedJobDetail?.updated_at]);

  useEffect(() => {
    const loadSubtitleSources = async () => {
      if (!selectedJobDetail || selectedJobDetail.media_metadata?.has_video !== true) {
        setSubtitleStreams([]);
        setSelectedSubtitleStreamIndex("");
        setSidecarSubtitles([]);
        return;
      }

      try {
        const [streamResponse, sidecarResponse] = await Promise.all([
          fetch(`${API_BASE_URL}/jobs/${selectedJobDetail.job_id}/subtitle-streams`),
          fetch(`${API_BASE_URL}/jobs/${selectedJobDetail.job_id}/sidecar-subtitles`),
        ]);

        if (streamResponse.ok) {
          const streamData = (await streamResponse.json()) as SubtitleStream[];
          setSubtitleStreams(streamData);
          setSelectedSubtitleStreamIndex(
            streamData.length > 0 && streamData[0]?.index !== undefined
              ? String(streamData[0].index)
              : "",
          );
        } else {
          setSubtitleStreams([]);
          setSelectedSubtitleStreamIndex("");
        }

        if (sidecarResponse.ok) {
          const sidecarData = (await sidecarResponse.json()) as SidecarSubtitle[];
          setSidecarSubtitles(sidecarData);
        } else {
          setSidecarSubtitles([]);
        }
      } catch {
        setSubtitleStreams([]);
        setSelectedSubtitleStreamIndex("");
        setSidecarSubtitles([]);
      }
    };

    void loadSubtitleSources();
  }, [selectedJobDetail?.job_id, selectedJobDetail?.updated_at]);

  const editorJob = selectedJobDetail ?? selectedJob;

  useEffect(() => {
    if (!editorJob) {
      setSoftsubOutputDir("");
      setSoftsubOutputName("");
      setSoftsubLanguage("eng");
      setSoftsubLabel("English Subtitles");
      setAdditionalTrackDrafts([]);
      return;
    }
    const mediaPathValue = editorJob.media_path;
    const lastSlashIndex = Math.max(mediaPathValue.lastIndexOf("/"), mediaPathValue.lastIndexOf("\\"));
    const directory = lastSlashIndex >= 0 ? mediaPathValue.slice(0, lastSlashIndex) : "";
    const baseName = editorJob.media_metadata?.file_name ?? `${editorJob.job_id}.mp4`;
    const dotIndex = baseName.lastIndexOf(".");
    const stem = dotIndex > 0 ? baseName.slice(0, dotIndex) : baseName;
    setSoftsubOutputDir(directory);
    setSoftsubOutputName(`${stem}.softsubs.mp4`);
    setSoftsubLanguage("eng");
    setSoftsubLabel("English Subtitles");
    setAdditionalTrackDrafts([]);
  }, [editorJob?.job_id]);
  const transcriptSegments = segmentDrafts;
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

    try {
      let response: globalThis.Response;
      if (selectedFile) {
        const formData = new FormData();
        formData.append("file", selectedFile);
        if (mediaPath.trim() && !isFakeBrowserPath(mediaPath.trim())) {
          formData.append("original_path", mediaPath.trim());
        }
        response = await fetch(`${API_BASE_URL}/ingest/upload`, {
          method: "POST",
          body: formData,
        });
      } else {
        response = await fetch(`${API_BASE_URL}/ingest`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ media_path: mediaPath }),
        });
      }
      if (!response.ok) {
        throw new Error(await readErrorMessage(response));
      }
      const data = (await response.json()) as IngestResponse;
      setIngestMessageIsError(false);
      setIngestMessage(data.message);
      setMediaPath("");
      setSelectedFileName("");
      setSelectedFile(null);
      await loadJobs();
      setSelectedJobId(data.job_id);
    } catch (error) {
      setIngestMessageIsError(true);
      setIngestMessage(
        error instanceof Error ? error.message : "Failed to queue ingest.",
      );
    }
  };

  const onSelectLocalFile = (event: ChangeEvent<HTMLInputElement>) => {
    const nextSelectedFile = event.target.files?.[0];
    if (!nextSelectedFile) {
      setSelectedFile(null);
      setSelectedFileName("");
      return;
    }

    setSelectedFile(nextSelectedFile);
    setSelectedFileName(nextSelectedFile.name);
    const resolvedPath = resolveLocalFilePath(nextSelectedFile, event.target.value);
    if (resolvedPath) {
      setMediaPath(resolvedPath);
    } else {
      setMediaPath("");
    }
    setIngestMessage("");
    setIngestMessageIsError(false);
  };

  const onImportSubtitleFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const subtitleFile = event.target.files?.[0];
    if (!subtitleFile || !selectedJobDetail) {
      return;
    }

    setIsImportingSubtitles(true);
    setSubtitleImportFileName(subtitleFile.name);
    setSegmentSaveMessage("");
    setSegmentSaveMessageIsError(false);
    try {
      const formData = new FormData();
      formData.append("file", subtitleFile);
      const response = await fetch(
        `${API_BASE_URL}/jobs/${selectedJobDetail.job_id}/import-subtitles`,
        {
          method: "POST",
          body: formData,
        },
      );
      if (!response.ok) {
        throw new Error(await readErrorMessage(response));
      }
      const data = (await response.json()) as JobDetail;
      setSelectedJobDetail(data);
      setSegmentDrafts(data.transcript_segments);
      await loadJobs();
      setSegmentSaveMessage(`Imported subtitles from ${subtitleFile.name}.`);
      setSegmentSaveMessageIsError(false);
    } catch (error) {
      setSegmentSaveMessage(
        error instanceof Error ? error.message : "Failed to import subtitle file.",
      );
      setSegmentSaveMessageIsError(true);
    } finally {
      event.target.value = "";
      setIsImportingSubtitles(false);
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
      const response = await fetch(
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
      await loadJobs();
      setSegmentSaveMessage("Imported embedded subtitle track.");
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
      const response = await fetch(
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
      await loadJobs();
      setSegmentSaveMessage("Imported sidecar subtitle file.");
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
      const response = await fetch(`${API_BASE_URL}/jobs/${jobId}/advance`, {
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

  const onApplyGlobalShift = () => {
    const shiftValue = Number.parseFloat(globalShiftSeconds);
    if (!Number.isFinite(shiftValue) || shiftValue === 0) {
      return;
    }

    setSegmentDrafts((current) =>
      current.map((segment) => {
        const nextStart = Math.max(0, Number((segment.start_seconds + shiftValue).toFixed(3)));
        const shiftedDuration = Math.max(0.01, segment.end_seconds - segment.start_seconds);
        const candidateEnd = Number((segment.end_seconds + shiftValue).toFixed(3));
        const nextEnd = candidateEnd <= nextStart
          ? Number((nextStart + shiftedDuration).toFixed(3))
          : candidateEnd;
        return {
          ...segment,
          start_seconds: nextStart,
          end_seconds: nextEnd,
        };
      }),
    );
    setSegmentSaveMessage(`Shifted all subtitle timings by ${shiftValue.toFixed(3)}s.`);
    setSegmentSaveMessageIsError(false);
    setGlobalShiftSeconds("5.0");
  };

  const onSaveSegments = async () => {
    if (!selectedJobDetail) {
      return;
    }
    setIsSavingSegments(true);
    setSegmentSaveMessage("");
    setSegmentSaveMessageIsError(false);
    try {
      const response = await fetch(
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
      setSegmentSaveMessage("Transcript edits saved.");
      setSegmentSaveMessageIsError(false);
    } catch (error) {
      setSegmentSaveMessage(
        error instanceof Error ? error.message : "Failed to save transcript edits.",
      );
      setSegmentSaveMessageIsError(true);
    } finally {
      setIsSavingSegments(false);
    }
  };

  const onExport = async (format: "srt" | "vtt") => {
    if (!editorJob) {
      return;
    }
    setExportingFormat(format);
    setExportMessage("");
    setExportMessageIsError(false);
    try {
      const response = await fetch(`${API_BASE_URL}/jobs/${editorJob.job_id}/export/${format}`);
      if (!response.ok) {
        throw new Error(await readErrorMessage(response));
      }
      const blob = await response.blob();
      downloadBlobFile(blob, `${editorJob.job_id}.${format}`);
      setExportMessage(`Downloaded ${editorJob.job_id}.${format}`);
      setExportMessageIsError(false);
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
      const response = await fetch(`${API_BASE_URL}/jobs/${selectedJobDetail.job_id}/subtitle-tracks`, {
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
      const response = await fetch(`${API_BASE_URL}/jobs/${selectedJobDetail.job_id}/subtitle-tracks/${trackId}`, {
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

  const onExportSoftsubMp4 = async () => {
    if (!editorJob) {
      return;
    }
    const outputDir = softsubOutputDir.trim();
    const outputName = softsubOutputName.trim();
    if (!outputDir || !outputName) {
      setExportMessageIsError(true);
      setExportMessage("Choose both output directory and output file name.");
      return;
    }

    const separator = outputDir.endsWith("/") || outputDir.endsWith("\\") ? "" : "/";
    const outputPath = `${outputDir}${separator}${outputName}`;
    setExportingFormat("mp4-softsub");
    setExportMessage("");
    setExportMessageIsError(false);
    try {
      const response = await fetch(`${API_BASE_URL}/jobs/${editorJob.job_id}/export/mp4-softsub`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          output_path: outputPath,
          track_ids: selectedJobDetail?.subtitle_tracks
            ?.filter((track) => track.is_active)
            .map((track) => track.track_id) ?? [],
        }),
      });
      if (!response.ok) {
        throw new Error(await readErrorMessage(response));
      }
      const data = (await response.json()) as SoftSubtitleExportResponse;
      setExportMessage(data.message);
      setExportMessageIsError(false);
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
        <p>Local-first placeholder workstation</p>
        <p>
          Backend: <strong>{backendStatus}</strong> (`{API_BASE_URL}`)
        </p>
      </header>

      <main className="workstation-grid">
        <section className="left-column">
          <Panel title="Media Import">
            <p>Queue local media ingest placeholder jobs.</p>
            <form className="ingest-form" onSubmit={onIngest}>
              <input
                className="media-input"
                type="text"
                value={mediaPath}
                placeholder="/path/to/local/media.mp4"
                onChange={(event) => setMediaPath(event.target.value)}
              />
              <input type="file" className="media-picker" onChange={onSelectLocalFile} />
              <button type="submit" disabled={!canSubmitIngest}>
                Submit Ingest
              </button>
            </form>
            <p className="muted">
              Pick a file to upload directly, or paste a local path manually. Submit stays disabled until one of those is present.
            </p>
            {selectedFileName ? (
              <p className="muted">Selected file: {selectedFileName}</p>
            ) : null}
            {ingestMessage ? (
              <p className={`status-message ${ingestMessageIsError ? "error" : ""}`}>
                {ingestMessage}
              </p>
            ) : null}
          </Panel>

          <Panel title="Jobs">
            {jobs.length === 0 ? <p>No jobs queued yet.</p> : null}
            <ul className="job-list">
              {jobs.map((job) => {
                const isSelected = selectedJobId === job.job_id;
                const isReady = job.stage === "ready";
                return (
                  <li key={job.job_id}>
                    <div
                      className={`job-card ${isSelected ? "selected" : ""}`}
                      onClick={() => setSelectedJobId(job.job_id)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          setSelectedJobId(job.job_id);
                        }
                      }}
                      role="button"
                      tabIndex={0}
                    >
                      <div className="job-card-top">
                        <span className="job-id">{job.job_id}</span>
                        <span className={`stage-pill stage-${job.stage}`}>{job.stage}</span>
                      </div>
                      <div className="job-media-path">{job.media_path}</div>
                      <div className="job-meta-row">
                        <span>{job.kind}</span>
                        <span>{job.progress_percent}%</span>
                      </div>
                      <div className="progress-track" aria-hidden="true">
                        <div
                          className="progress-fill"
                          style={{ width: `${job.progress_percent}%` }}
                        />
                      </div>
                      <div className="job-actions">
                        <button
                          type="button"
                          className="secondary-btn"
                          onClick={(event) => {
                            event.stopPropagation();
                            void onAdvanceJob(job.job_id);
                          }}
                          disabled={advancingJobId === job.job_id || isReady}
                        >
                          {isReady
                            ? "Ready"
                            : advancingJobId === job.job_id
                              ? "Advancing..."
                              : "Advance"}
                        </button>
                      </div>
                    </div>
                  </li>
                );
              })}
            </ul>
          </Panel>
        </section>

        <section className="right-column">
          <Panel title="Waveform / Editor">
            <div className="editor-placeholder">
              <p>Waveform timeline placeholder</p>
              <div className="waveform-strip" aria-hidden="true">
                <div className="waveform-fill" />
              </div>
              {isLoadingJobDetail ? <p className="muted">Loading job detail...</p> : null}
              {jobDetailError ? <p className="status-message error">{jobDetailError}</p> : null}
              {editorJob ? (
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
              ) : (
                <p className="muted">Select a job to inspect placeholder metadata.</p>
              )}
              {editorJob ? (
                <div className="subtitle-panel">
                  <div className="subtitle-panel-header">
                    <p className="subtitle-panel-title">Subtitle Segments</p>
                    <div className="subtitle-panel-actions">
                      <label className="secondary-btn file-action-btn">
                        {isImportingSubtitles ? "Importing..." : "Import SRT/VTT"}
                        <input
                          type="file"
                          accept=".srt,.vtt,text/vtt,application/x-subrip"
                          onChange={onImportSubtitleFile}
                          disabled={!selectedJobDetail || isImportingSubtitles || isSavingSegments}
                        />
                      </label>
                      {sidecarSubtitles.length > 0 ? (
                        <button
                          type="button"
                          className="secondary-btn"
                          onClick={() => void onImportSidecarSubtitles()}
                          disabled={
                            !selectedJobDetail ||
                            isImportingSidecarSubtitles ||
                            isImportingSubtitles ||
                            isImportingEmbeddedSubtitles ||
                            isSavingSegments
                          }
                        >
                          {isImportingSidecarSubtitles
                            ? "Importing sidecar..."
                            : `Import Sidecar (${sidecarSubtitles[0]?.file_name ?? "found"})`}
                        </button>
                      ) : (
                        <span className="muted subtitle-source-note">No sidecar .srt/.vtt found</span>
                      )}
                      {subtitleStreams.length > 0 ? (
                        <>
                          <select
                            className="subtitle-stream-select"
                            value={selectedSubtitleStreamIndex}
                            onChange={(event) => setSelectedSubtitleStreamIndex(event.target.value)}
                            disabled={isImportingEmbeddedSubtitles || isSavingSegments}
                          >
                            {subtitleStreams.map((stream) => (
                              <option key={stream.index} value={stream.index}>
                                Stream {stream.index}
                                {stream.language ? ` · ${stream.language}` : ""}
                                {stream.title ? ` · ${stream.title}` : ""}
                                {stream.codec_name ? ` · ${stream.codec_name}` : ""}
                              </option>
                            ))}
                          </select>
                          <button
                            type="button"
                            className="secondary-btn"
                            onClick={() => void onImportEmbeddedSubtitles()}
                            disabled={
                              !selectedJobDetail ||
                              isImportingEmbeddedSubtitles ||
                              isImportingSubtitles ||
                              isImportingSidecarSubtitles ||
                              isSavingSegments ||
                              !selectedSubtitleStreamIndex
                            }
                          >
                            {isImportingEmbeddedSubtitles
                              ? "Extracting..."
                              : "Import Embedded Track"}
                          </button>
                        </>
                      ) : (
                        <span className="muted subtitle-source-note">No embedded subtitle tracks found</span>
                      )}
                      <button
                        type="button"
                        className="secondary-btn"
                        onClick={() => void onSaveSegments()}
                        disabled={
                          !selectedJobDetail ||
                          isLoadingJobDetail ||
                          isSavingSegments ||
                          isImportingSubtitles ||
                          isImportingEmbeddedSubtitles ||
                          !hasUnsavedSegmentChanges
                        }
                      >
                        {isSavingSegments ? "Saving..." : "Save Segments"}
                      </button>
                    </div>
                  </div>
                  <div className="global-shift-box">
                    <label className="subtitle-edit-field">
                      <span>Shift All Subtitles (seconds)</span>
                      <input
                        type="number"
                        step="0.1"
                        value={globalShiftSeconds}
                        onChange={(event) => setGlobalShiftSeconds(event.target.value)}
                        disabled={!selectedJobDetail || isSavingSegments}
                      />
                    </label>
                    <button
                      type="button"
                      className="secondary-btn"
                      onClick={() => void onApplyGlobalShift()}
                      disabled={
                        !selectedJobDetail ||
                        isSavingSegments ||
                        !Number.isFinite(Number.parseFloat(globalShiftSeconds)) ||
                        Number.parseFloat(globalShiftSeconds) === 0
                      }
                    >
                      Apply Shift
                    </button>
                  </div>
                  {subtitleImportFileName ? (
                    <p className="muted">Latest subtitle import file: {subtitleImportFileName}</p>
                  ) : null}
                  {segmentSaveMessage ? (
                    <p className={`status-message ${segmentSaveMessageIsError ? "error" : ""}`}>
                      {segmentSaveMessage}
                    </p>
                  ) : null}
                  {transcriptSegments.length === 0 ? (
                    <p className="muted">No subtitle segments available yet.</p>
                  ) : (
                    <>
                      <ul className="subtitle-list">
                        {transcriptSegments.map((segment, index) => {
                          const duration = segment.end_seconds - segment.start_seconds;
                          return (
                            <li className="subtitle-item" key={segment.segment_id}>
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
                                <div className="subtitle-time">
                                  {formatTimecode(segment.start_seconds)} -{" "}
                                  {formatTimecode(segment.end_seconds)}
                                  <span className="subtitle-duration">
                                    {duration.toFixed(2)}s
                                  </span>
                                </div>
                              </div>
                              <label className="subtitle-edit-field subtitle-text-field">
                                <span>Text</span>
                                <textarea
                                  value={segment.text}
                                  onChange={(event) => onSegmentTextChange(index, event.target.value)}
                                  rows={2}
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
                            </li>
                          );
                        })}
                      </ul>
                      <div className="subtitle-panel-header">
                        <button
                          type="button"
                          className="secondary-btn"
                          onClick={() => void onSaveSegments()}
                          disabled={
                            !selectedJobDetail ||
                            isLoadingJobDetail ||
                            isSavingSegments ||
                            isImportingSubtitles ||
                            isImportingEmbeddedSubtitles ||
                            !hasUnsavedSegmentChanges
                          }
                        >
                          {isSavingSegments ? "Saving..." : "Save Segments"}
                        </button>
                      </div>
                    </>
                  )}
                </div>
              ) : null}
            </div>
            {/* TODO: Integrate waveform renderer and segment editing interactions. */}
            {/* TODO: Overlay speaker segments from Pyannote diarization. */}
          </Panel>

          <Panel title="Export">
            <p>Export subtitles and rendered outputs.</p>
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
                <span>Output Directory</span>
                <input
                  type="text"
                  value={softsubOutputDir}
                  onChange={(event) => setSoftsubOutputDir(event.target.value)}
                  placeholder="/Users/kuon/Desktop"
                  disabled={!editorJob || exportingFormat !== null}
                />
              </label>
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
            {exportMessage ? (
              <p className={`status-message ${exportMessageIsError ? "error" : ""}`}>
                {exportMessage}
              </p>
            ) : null}
          </Panel>
        </section>
      </main>
    </div>
  );
}
