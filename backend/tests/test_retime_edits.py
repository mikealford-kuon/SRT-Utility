import asyncio
import io
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import UploadFile

from app import main as app_main
from app.main import (
    CorrectionSuggestion,
    JobDetail,
    TranscriptSegment,
    apply_correction_suggestions_to_text,
    parse_subtitle_text_to_segments,
    retime_edited_subtitle_segments,
)


def segment(segment_id: str, start: float, end: float, text: str) -> TranscriptSegment:
    return TranscriptSegment(
        segment_id=segment_id,
        start_seconds=start,
        end_seconds=end,
        text=text,
        speaker=None,
    )


class RetimeEditedSubtitleTests(unittest.TestCase):
    def test_exact_text_carries_old_edits_onto_new_timing(self) -> None:
        old_segments = [
            segment("old-1", 0.0, 2.0, "Welcome to the edited course."),
            segment("old-2", 2.0, 4.0, "This line has punctuation!"),
        ]
        new_segments = [
            segment("new-1", 1.2, 3.4, "Welcome to the edited course"),
            segment("new-2", 3.5, 6.2, "This line has punctuation"),
        ]

        retimed, report = retime_edited_subtitle_segments(
            old_segments=old_segments,
            new_timing_segments=new_segments,
            source_file_name="previous.vtt",
            source_format="vtt",
            threshold=0.58,
        )

        self.assertEqual([item.text for item in retimed], [item.text for item in old_segments])
        self.assertEqual(retimed[0].start_seconds, 1.2)
        self.assertEqual(retimed[1].end_seconds, 6.2)
        self.assertEqual(report.matched_segments, 2)
        self.assertEqual(report.low_confidence_segments, 0)

    def test_inserted_new_material_is_preserved_and_reported(self) -> None:
        old_segments = [
            segment("old-1", 0.0, 2.0, "First edited line"),
            segment("old-2", 2.0, 4.0, "Second edited line"),
        ]
        new_segments = [
            segment("new-1", 0.0, 1.5, "New intro that was inserted"),
            segment("new-2", 1.5, 3.0, "First edited line"),
            segment("new-3", 3.0, 5.0, "Second edited line"),
        ]

        retimed, report = retime_edited_subtitle_segments(
            old_segments=old_segments,
            new_timing_segments=new_segments,
            source_file_name="previous.vtt",
            source_format="vtt",
            threshold=0.58,
        )

        self.assertEqual(retimed[0].text, "New intro that was inserted")
        self.assertEqual(retimed[0].retime_status, "new-only")
        self.assertEqual(retimed[1].text, "First edited line")
        self.assertEqual(retimed[2].text, "Second edited line")
        self.assertEqual(report.matched_segments, 2)
        self.assertEqual(report.unmatched_new_segments, 1)

    def test_single_old_vtt_cue_splits_across_current_timing_segments(self) -> None:
        old_segments = [
            segment(
                "old-1",
                28.485,
                38.112,
                "The objective is to establish that a firm organizational process is in place to properly manage the technical schedule and cost components of the work.",
            ),
        ]
        new_segments = [
            segment(
                "new-1",
                29.396,
                35.338,
                "The objective is to establish that a firm organizational process is in place to properly manage the technical",
            ),
            segment(
                "new-2",
                36.139,
                39.001,
                "schedule and cost components of the work.",
            ),
        ]

        retimed, report = retime_edited_subtitle_segments(
            old_segments=old_segments,
            new_timing_segments=new_segments,
            source_file_name="previous.vtt",
            source_format="vtt",
            threshold=0.58,
        )

        self.assertEqual(
            retimed[0].text,
            "The objective is to establish that a firm organizational process is in place to properly manage the technical",
        )
        self.assertEqual(retimed[1].text, "schedule and cost components of the work.")
        self.assertNotIn(retimed[1].text, retimed[0].text)
        self.assertEqual(retimed[0].start_seconds, 29.396)
        self.assertEqual(retimed[1].end_seconds, 39.001)
        self.assertEqual(retimed[0].retime_status, "matched")
        self.assertEqual(retimed[1].retime_status, "matched")
        self.assertEqual(report.matched_segments, 2)
        self.assertEqual(report.unmatched_old_segments, 0)
        self.assertEqual(report.unmatched_new_segments, 0)

    def test_matched_long_legacy_cue_can_extend_early_current_end(self) -> None:
        old_segments = [
            segment(
                "old-1",
                10.0,
                20.0,
                "This long cue should remain visible until the spoken audio has actually finished.",
            ),
            segment("old-2", 22.0, 25.0, "The next subtitle starts later."),
        ]
        new_segments = [
            segment(
                "new-1",
                10.15,
                15.0,
                "This long cue should remain visible until the spoken audio has actually finished.",
            ),
            segment("new-2", 22.1, 24.8, "The next subtitle starts later."),
        ]

        retimed, report = retime_edited_subtitle_segments(
            old_segments=old_segments,
            new_timing_segments=new_segments,
            source_file_name="previous.vtt",
            source_format="vtt",
            threshold=0.58,
        )

        self.assertEqual(retimed[0].text, old_segments[0].text)
        self.assertEqual(retimed[0].start_seconds, 10.15)
        self.assertEqual(retimed[0].end_seconds, 20.0)
        self.assertIn("extended", retimed[0].retime_note or "")
        self.assertEqual(report.matched_segments, 2)

    def test_legacy_end_extension_is_capped_before_next_current_segment(self) -> None:
        old_segments = [
            segment("old-1", 10.0, 20.0, "This cue should not overlap the next one."),
            segment("old-2", 17.0, 21.0, "The next subtitle starts before the legacy cue ends."),
        ]
        new_segments = [
            segment("new-1", 10.1, 15.0, "This cue should not overlap the next one."),
            segment("new-2", 17.2, 21.0, "The next subtitle starts before the legacy cue ends."),
        ]

        retimed, _ = retime_edited_subtitle_segments(
            old_segments=old_segments,
            new_timing_segments=new_segments,
            source_file_name="previous.vtt",
            source_format="vtt",
            threshold=0.58,
        )

        self.assertAlmostEqual(retimed[0].end_seconds, 17.18)
        self.assertLess(retimed[0].end_seconds, retimed[1].start_seconds)

    def test_split_legacy_cue_can_extend_last_split_segment_end(self) -> None:
        old_segments = [
            segment(
                "old-1",
                28.5,
                40.0,
                "The objective is to establish control account traceability across scope schedule and cost components.",
            ),
        ]
        new_segments = [
            segment(
                "new-1",
                28.7,
                33.0,
                "The objective is to establish control account traceability across scope",
            ),
            segment(
                "new-2",
                33.4,
                36.0,
                "schedule and cost components.",
            ),
            segment(
                "new-3",
                42.0,
                45.0,
                "New unrelated material after the cue.",
            ),
        ]

        retimed, report = retime_edited_subtitle_segments(
            old_segments=old_segments,
            new_timing_segments=new_segments,
            source_file_name="previous.vtt",
            source_format="vtt",
            threshold=0.58,
        )

        self.assertEqual(retimed[0].end_seconds, 33.0)
        self.assertEqual(retimed[1].end_seconds, 40.0)
        self.assertIn("extended", retimed[1].retime_note or "")
        self.assertEqual(retimed[2].retime_status, "new-only")
        self.assertEqual(report.matched_segments, 2)

    def test_multiple_old_vtt_cues_merge_into_one_current_timing_segment(self) -> None:
        old_segments = [
            segment("old-1", 10.0, 12.0, "First corrected sentence."),
            segment("old-2", 12.2, 14.0, "Second corrected sentence."),
            segment("old-3", 15.0, 17.0, "Third corrected sentence."),
        ]
        new_segments = [
            segment(
                "new-1",
                10.1,
                14.2,
                "First corrected sentence Second corrected sentence",
            ),
            segment("new-2", 15.1, 17.1, "Third corrected sentence"),
        ]

        retimed, report = retime_edited_subtitle_segments(
            old_segments=old_segments,
            new_timing_segments=new_segments,
            source_file_name="previous.vtt",
            source_format="vtt",
            threshold=0.58,
        )

        self.assertEqual(retimed[0].text, "First corrected sentence.\nSecond corrected sentence.")
        self.assertEqual(retimed[0].retime_status, "matched")
        self.assertIn("merged", retimed[0].retime_note or "")
        self.assertEqual(retimed[1].text, "Third corrected sentence.")
        self.assertEqual(report.matched_segments, 2)
        self.assertEqual(report.unmatched_old_segments, 0)

    def test_reapplying_old_vtt_repairs_already_duplicated_tail_segment(self) -> None:
        old_segments = [
            segment(
                "old-1",
                28.485,
                38.112,
                "The objective is to establish that a firm organizational process is in place to properly manage the technical schedule and cost components of the work.",
            ),
        ]
        polluted_segments = [
            segment(
                "new-1",
                29.396,
                35.338,
                "The objective is to establish that a firm organizational process is in place to properly manage the technical schedule and cost components of the work.",
            ),
            segment(
                "new-2",
                36.139,
                39.001,
                "schedule and cost components of the work.",
            ),
        ]

        retimed, report = retime_edited_subtitle_segments(
            old_segments=old_segments,
            new_timing_segments=polluted_segments,
            source_file_name="previous.vtt",
            source_format="vtt",
            threshold=0.58,
        )

        self.assertEqual(
            retimed[0].text,
            "The objective is to establish that a firm organizational process is in place to properly manage the technical",
        )
        self.assertEqual(retimed[1].text, "schedule and cost components of the work.")
        self.assertEqual(retimed[0].retime_status, "matched")
        self.assertEqual(retimed[1].retime_status, "matched")
        self.assertEqual(report.matched_segments, 2)
        self.assertEqual(report.unmatched_new_segments, 0)

    def test_split_legacy_cue_when_best_match_is_tail_segment(self) -> None:
        old_segments = [
            segment(
                "old-1",
                28.485,
                38.112,
                "The objective is to establish that a firm organizational process is in place to properly manage the technical schedule and cost components of the work.",
            ),
        ]
        new_segments = [
            segment(
                "new-1",
                29.396,
                35.338,
                "The objective is to establish that a firm organizational process",
            ),
            segment(
                "new-2",
                36.139,
                39.001,
                "process is in place to properly manage the technical schedule and cost components of the work.",
            ),
        ]

        retimed, report = retime_edited_subtitle_segments(
            old_segments=old_segments,
            new_timing_segments=new_segments,
            source_file_name="previous.vtt",
            source_format="vtt",
            threshold=0.58,
        )

        joined = " ".join(item.text.replace("\n", " ") for item in retimed)
        self.assertEqual(joined.count("The objective"), 1)
        self.assertEqual(joined.count("process is in place"), 1)
        self.assertEqual(retimed[0].text, "The objective is to establish that a firm organizational")
        self.assertEqual(
            retimed[1].text,
            "process is in place to properly manage the technical schedule and cost components of the work.",
        )
        self.assertEqual(retimed[0].retime_status, "matched")
        self.assertEqual(retimed[1].retime_status, "matched")
        self.assertEqual(report.matched_segments, 2)
        self.assertEqual(report.unmatched_new_segments, 0)

    def test_split_legacy_cue_repairs_repeated_boundary_words(self) -> None:
        old_segments = [
            segment(
                "old-1",
                10.0,
                18.0,
                "Alpha beta gamma delta epsilon zeta eta theta.",
            ),
        ]
        new_segments = [
            segment("new-1", 10.1, 13.2, "Alpha beta gamma delta"),
            segment("new-2", 13.3, 18.1, "gamma delta epsilon zeta eta theta"),
        ]

        retimed, report = retime_edited_subtitle_segments(
            old_segments=old_segments,
            new_timing_segments=new_segments,
            source_file_name="previous.vtt",
            source_format="vtt",
            threshold=0.58,
        )

        joined_tokens = " ".join(item.text for item in retimed).lower().replace(".", "").split()
        self.assertEqual(joined_tokens, ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"])
        self.assertEqual(retimed[0].text, "Alpha beta")
        self.assertEqual(retimed[1].text, "gamma delta epsilon zeta eta theta.")
        self.assertEqual(report.matched_segments, 2)
        self.assertEqual(report.unmatched_new_segments, 0)

    def test_unrelated_previous_file_does_not_overwrite_new_text(self) -> None:
        old_segments = [
            segment("old-1", 0.0, 2.0, "A totally unrelated script about finance"),
        ]
        new_segments = [
            segment("new-1", 0.0, 1.5, "Welcome to the training video"),
        ]

        retimed, report = retime_edited_subtitle_segments(
            old_segments=old_segments,
            new_timing_segments=new_segments,
            source_file_name="wrong.vtt",
            source_format="vtt",
            threshold=0.58,
        )

        self.assertEqual(retimed[0].text, "Welcome to the training video")
        self.assertEqual(retimed[0].retime_status, "low-confidence")
        self.assertEqual(report.matched_segments, 0)
        self.assertEqual(report.low_confidence_segments, 1)

    def test_vtt_parser_accepts_cue_settings_after_end_timestamp(self) -> None:
        segments = parse_subtitle_text_to_segments(
            content=(
                "WEBVTT\n\n"
                "00:00:01.000 --> 00:00:03.500 align:start position:0%\n"
                "Caption with VTT cue settings.\n"
            ),
            format_name="vtt",
            job_id="settings-test",
        )

        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].start_seconds, 1.0)
        self.assertEqual(segments[0].end_seconds, 3.5)
        self.assertEqual(segments[0].text, "Caption with VTT cue settings.")

    def test_learned_correction_auto_applies_to_inserted_new_material(self) -> None:
        old_segments = [
            segment("old-1", 0.0, 2.0, "We use OpenClaw Runtime today."),
        ]
        new_segments = [
            segment("new-1", 0.0, 2.0, "We use open claw runtime today"),
            segment("new-2", 2.0, 4.0, "The open claw dashboard is new"),
        ]

        retimed, report = retime_edited_subtitle_segments(
            old_segments=old_segments,
            new_timing_segments=new_segments,
            source_file_name="previous.vtt",
            source_format="vtt",
            threshold=0.58,
        )

        self.assertEqual(retimed[0].text, "We use OpenClaw Runtime today.")
        self.assertEqual(retimed[1].text, "The OpenClaw dashboard is new")
        self.assertEqual(retimed[1].retime_status, "corrected")
        self.assertGreaterEqual(report.applied_corrections, 1)
        self.assertIn(
            ("open claw", "OpenClaw"),
            {(item.wrong_text, item.corrected_text) for item in report.learned_corrections},
        )

    def test_uncertain_correction_is_flagged_as_sore_thumb(self) -> None:
        corrected_text, applied, suggested = apply_correction_suggestions_to_text(
            "The crew on dashboard is new",
            [
                CorrectionSuggestion(
                    wrong_text="crew on",
                    corrected_text="Kuon",
                    confidence=0.64,
                    kind="llm-candidate",
                    status="suggested",
                    source_segment_id="new-1",
                )
            ],
        )

        self.assertEqual(corrected_text, "The crew on dashboard is new")
        self.assertEqual(applied, [])
        self.assertEqual(len(suggested), 1)
        self.assertEqual(suggested[0].corrected_text, "Kuon")

    def test_llm_candidate_correction_applies_to_inserted_material(self) -> None:
        old_segments = [
            segment("old-1", 0.0, 2.0, "The Kuon workspace shipped today."),
        ]
        new_segments = [
            segment("new-1", 0.0, 2.0, "The crew on workspace shipped today"),
            segment("new-2", 2.0, 4.0, "The crew on dashboard is new"),
        ]

        def fake_llm_provider(
            raw_text: str,
            corrected_text: str,
            confidence: float,
            source_segment_id: str | None,
        ) -> list[CorrectionSuggestion]:
            self.assertEqual(raw_text, "The crew on workspace shipped today")
            self.assertEqual(corrected_text, "The Kuon workspace shipped today.")
            return [
                CorrectionSuggestion(
                    wrong_text="crew on",
                    corrected_text="Kuon",
                    confidence=0.94,
                    kind="llm-candidate",
                    status="suggested",
                    source_segment_id=source_segment_id,
                )
            ]

        retimed, report = retime_edited_subtitle_segments(
            old_segments=old_segments,
            new_timing_segments=new_segments,
            source_file_name="previous.vtt",
            source_format="vtt",
            threshold=0.58,
            llm_correction_provider=fake_llm_provider,
        )

        self.assertEqual(retimed[0].text, "The Kuon workspace shipped today.")
        self.assertEqual(retimed[1].text, "The Kuon dashboard is new")
        self.assertEqual(retimed[1].retime_status, "corrected")
        self.assertIn(
            ("crew on", "Kuon", "llm-candidate"),
            {(item.wrong_text, item.corrected_text, item.kind) for item in report.learned_corrections},
        )

    def test_removed_old_material_is_reported_without_forcing_it_in(self) -> None:
        old_segments = [
            segment("old-1", 0.0, 1.0, "Kept edited line"),
            segment("old-2", 1.0, 2.0, "This old line was removed"),
        ]
        new_segments = [
            segment("new-1", 10.0, 11.0, "Kept edited line"),
        ]

        retimed, report = retime_edited_subtitle_segments(
            old_segments=old_segments,
            new_timing_segments=new_segments,
            source_file_name="previous.vtt",
            source_format="vtt",
            threshold=0.58,
        )

        self.assertEqual(len(retimed), 1)
        self.assertEqual(retimed[0].text, "Kept edited line")
        self.assertEqual(report.unmatched_old_segments, 1)


class RetimeEditedSubtitleEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_jobs = list(app_main.jobs)
        self.original_data_dir = app_main.DATA_DIR
        self.original_uploads_dir = app_main.UPLOADS_DIR
        self.original_store_path = app_main.STORE_PATH
        app_main.DATA_DIR = Path(self.temp_dir.name)
        app_main.UPLOADS_DIR = app_main.DATA_DIR / "uploads"
        app_main.STORE_PATH = app_main.DATA_DIR / "jobs.json"
        now = datetime.now(timezone.utc).isoformat()
        app_main.jobs[:] = [
            JobDetail(
                job_id="job-retime-test",
                kind="video",
                media_path="/tmp/new-video.mp4",
                stage="ready",
                progress_percent=100,
                stage_label="Ready",
                stage_description="Processing complete.",
                created_at=now,
                updated_at=now,
                transcript_segments=[
                    segment("new-1", 1.0, 2.5, "Welcome to the edited course"),
                    segment("new-2", 2.6, 4.0, "This line has punctuation"),
                ],
            )
        ]

    def tearDown(self) -> None:
        app_main.jobs[:] = self.original_jobs
        app_main.DATA_DIR = self.original_data_dir
        app_main.UPLOADS_DIR = self.original_uploads_dir
        app_main.STORE_PATH = self.original_store_path
        self.temp_dir.cleanup()

    def test_endpoint_applies_previous_text_and_returns_report(self) -> None:
        previous_vtt = (
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:01.000\n"
            "Welcome to the edited course.\n\n"
            "00:00:01.000 --> 00:00:02.000\n"
            "This line has punctuation!\n"
        )

        upload = UploadFile(
            file=io.BytesIO(previous_vtt.encode("utf-8")),
            filename="previous.vtt",
        )
        response = asyncio.run(
            app_main.retime_job_edited_subtitles(
                "job-retime-test",
                file=upload,
                confidence_threshold=0.58,
            )
        )

        payload = response.model_dump()
        self.assertEqual(payload["retime_report"]["matched_segments"], 2)
        self.assertEqual(payload["transcript_segments"][0]["text"], "Welcome to the edited course.")
        self.assertEqual(payload["transcript_segments"][0]["start_seconds"], 1.0)
        self.assertEqual(payload["transcript_segments"][1]["end_seconds"], 4.0)
        self.assertIn(
            "pre-retime-snapshot",
            {track["source_kind"] for track in payload["subtitle_tracks"]},
        )
        self.assertIn(
            "retimed-edits",
            {track["source_kind"] for track in payload["subtitle_tracks"]},
        )

    def test_endpoint_repairs_tail_match_split_without_duplicates(self) -> None:
        app_main.jobs[0] = app_main.jobs[0].model_copy(
            update={
                "transcript_segments": [
                    segment(
                        "new-1",
                        29.396,
                        35.338,
                        "The objective is to establish that a firm organizational process",
                    ),
                    segment(
                        "new-2",
                        36.139,
                        39.001,
                        "process is in place to properly manage the technical schedule and cost components of the work.",
                    ),
                ],
            }
        )
        previous_vtt = (
            "WEBVTT\n\n"
            "00:00:28.485 --> 00:00:38.112\n"
            "The objective is to establish that a firm organizational process is in place to properly manage the technical schedule and cost components of the work.\n"
        )

        upload = UploadFile(
            file=io.BytesIO(previous_vtt.encode("utf-8")),
            filename="previous.vtt",
        )
        response = asyncio.run(
            app_main.retime_job_edited_subtitles(
                "job-retime-test",
                file=upload,
                confidence_threshold=0.58,
            )
        )

        payload = response.model_dump()
        retimed_text = " ".join(segment["text"] for segment in payload["transcript_segments"])
        self.assertEqual(retimed_text.count("The objective"), 1)
        self.assertEqual(retimed_text.count("process is in place"), 1)
        self.assertEqual(payload["retime_report"]["matched_segments"], 2)
        self.assertEqual(payload["retime_report"]["unmatched_new_segments"], 0)

    def test_sidecar_import_applies_text_to_existing_timing(self) -> None:
        media_path = Path(self.temp_dir.name) / "changed-video.mp4"
        media_path.write_bytes(b"placeholder")
        sidecar_path = media_path.with_suffix(".vtt")
        sidecar_path.write_text(
            "WEBVTT\n\n"
            "00:00:50.000 --> 00:00:55.000\n"
            "Welcome to the edited course.\n\n"
            "00:00:55.000 --> 00:01:00.000\n"
            "This line has punctuation!\n",
            encoding="utf-8",
        )
        app_main.jobs[0] = app_main.jobs[0].model_copy(update={"media_path": str(media_path)})

        response = app_main.import_sidecar_subtitles("job-retime-test")

        payload = response.model_dump()
        self.assertEqual(payload["transcript_segments"][0]["text"], "Welcome to the edited course.")
        self.assertEqual(payload["transcript_segments"][0]["start_seconds"], 1.0)
        self.assertEqual(payload["transcript_segments"][1]["end_seconds"], 4.0)
        self.assertIn(
            "pre-retime-snapshot",
            {track["source_kind"] for track in payload["subtitle_tracks"]},
        )
        self.assertIn(
            "sidecar-subtitle",
            {track["source_kind"] for track in payload["subtitle_tracks"]},
        )

    def test_initial_upload_can_store_legacy_vtt_for_auto_apply(self) -> None:
        captured: dict[str, object] = {}
        original_create_ingest_job = app_main.create_ingest_job

        def fake_create_ingest_job(
            *,
            media_path: Path,
            pending_legacy_subtitle_path: Path | None = None,
            pending_legacy_subtitle_name: str | None = None,
        ) -> app_main.IngestResponse:
            captured["media_path"] = media_path
            captured["pending_legacy_subtitle_path"] = pending_legacy_subtitle_path
            captured["pending_legacy_subtitle_name"] = pending_legacy_subtitle_name
            return app_main.IngestResponse(
                job_id="job-upload-test",
                status="queued",
                message="queued",
            )

        try:
            app_main.create_ingest_job = fake_create_ingest_job
            response = asyncio.run(
                app_main.ingest_upload(
                    file=UploadFile(file=io.BytesIO(b"video-bytes"), filename="video.mp4"),
                    legacy_subtitle=UploadFile(
                        file=io.BytesIO(
                            b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nCorrected text.\n"
                        ),
                        filename="legacy.vtt",
                    ),
                )
            )
        finally:
            app_main.create_ingest_job = original_create_ingest_job

        self.assertEqual(response.job_id, "job-upload-test")
        self.assertEqual(captured["pending_legacy_subtitle_name"], "legacy.vtt")
        pending_path = captured["pending_legacy_subtitle_path"]
        self.assertIsInstance(pending_path, Path)
        self.assertTrue(Path(str(pending_path)).exists())
        self.assertIn("Corrected text", Path(str(pending_path)).read_text(encoding="utf-8"))

    def test_local_ingest_can_store_legacy_vtt_path_for_auto_apply(self) -> None:
        captured: dict[str, object] = {}
        media_path = Path(self.temp_dir.name) / "video.mp4"
        media_path.write_bytes(b"video-bytes")
        legacy_path = Path(self.temp_dir.name) / "legacy.vtt"
        legacy_path.write_text(
            "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nCorrected path text.\n",
            encoding="utf-8",
        )
        original_create_ingest_job = app_main.create_ingest_job

        def fake_create_ingest_job(
            *,
            media_path: Path,
            pending_legacy_subtitle_path: Path | None = None,
            pending_legacy_subtitle_name: str | None = None,
        ) -> app_main.IngestResponse:
            captured["media_path"] = media_path
            captured["pending_legacy_subtitle_path"] = pending_legacy_subtitle_path
            captured["pending_legacy_subtitle_name"] = pending_legacy_subtitle_name
            return app_main.IngestResponse(
                job_id="job-local-path-test",
                status="queued",
                message="queued",
            )

        try:
            app_main.create_ingest_job = fake_create_ingest_job
            response = app_main.ingest(
                app_main.IngestRequest(
                    media_path=str(media_path),
                    legacy_subtitle_path=str(legacy_path),
                )
            )
        finally:
            app_main.create_ingest_job = original_create_ingest_job

        self.assertEqual(response.job_id, "job-local-path-test")
        self.assertEqual(captured["pending_legacy_subtitle_path"], legacy_path)
        self.assertEqual(captured["pending_legacy_subtitle_name"], "legacy.vtt")

    def test_initial_upload_can_use_legacy_vtt_path_when_file_is_not_uploaded(self) -> None:
        captured: dict[str, object] = {}
        legacy_path = Path(self.temp_dir.name) / "legacy-from-path.vtt"
        legacy_path.write_text(
            "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nCorrected path upload text.\n",
            encoding="utf-8",
        )
        original_create_ingest_job = app_main.create_ingest_job

        def fake_create_ingest_job(
            *,
            media_path: Path,
            pending_legacy_subtitle_path: Path | None = None,
            pending_legacy_subtitle_name: str | None = None,
        ) -> app_main.IngestResponse:
            captured["media_path"] = media_path
            captured["pending_legacy_subtitle_path"] = pending_legacy_subtitle_path
            captured["pending_legacy_subtitle_name"] = pending_legacy_subtitle_name
            return app_main.IngestResponse(
                job_id="job-upload-path-test",
                status="queued",
                message="queued",
            )

        try:
            app_main.create_ingest_job = fake_create_ingest_job
            response = asyncio.run(
                app_main.ingest_upload(
                    file=UploadFile(file=io.BytesIO(b"video-bytes"), filename="video.mp4"),
                    legacy_subtitle_path=str(legacy_path),
                )
            )
        finally:
            app_main.create_ingest_job = original_create_ingest_job

        self.assertEqual(response.job_id, "job-upload-path-test")
        self.assertEqual(captured["pending_legacy_subtitle_path"], legacy_path)
        self.assertEqual(captured["pending_legacy_subtitle_name"], "legacy-from-path.vtt")

    def test_pending_legacy_vtt_auto_applies_after_timing_generation(self) -> None:
        pending_path = app_main.DATA_DIR / "tracks" / "startup-legacy.vtt"
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.write_text(
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:01.000\n"
            "Welcome to the edited course.\n\n"
            "00:00:01.000 --> 00:00:02.000\n"
            "This line has punctuation!\n",
            encoding="utf-8",
        )
        app_main.jobs[0] = app_main.jobs[0].model_copy(
            update={
                "pending_legacy_subtitle_path": str(pending_path),
                "pending_legacy_subtitle_name": "startup-legacy.vtt",
            }
        )

        app_main.apply_pending_legacy_subtitle("job-retime-test")

        payload = app_main.jobs[0].model_dump()
        self.assertEqual(payload["transcript_segments"][0]["text"], "Welcome to the edited course.")
        self.assertEqual(payload["transcript_segments"][1]["text"], "This line has punctuation!")
        self.assertIsNone(payload["pending_legacy_subtitle_path"])
        self.assertIn(
            "retimed-edits",
            {track["source_kind"] for track in payload["subtitle_tracks"]},
        )


class OperationalReadinessTests(unittest.TestCase):
    def test_diagnostics_reports_core_runtime_without_secrets(self) -> None:
        payload = app_main.diagnostics()

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["service"], "subtitle-workstation-api")
        self.assertIn("ffmpeg", payload["tools"])
        self.assertIn("whisperx", payload["tools"])
        self.assertIn("enabled", payload["llm"])
        self.assertNotIn("password", str(payload).lower())
        self.assertNotIn("secret", str(payload).lower())

    def test_ingest_worker_lock_serializes_processing(self) -> None:
        calls: list[str] = []
        original_worker = app_main.run_ingest_pipeline_unlocked
        original_active_job_id = app_main.active_ingest_job_id

        def fake_worker(job_id: str, media_path: Path) -> None:
            calls.append(f"start:{job_id}")
            time.sleep(0.05)
            calls.append(f"end:{job_id}")

        try:
            app_main.run_ingest_pipeline_unlocked = fake_worker
            first = threading.Thread(
                target=app_main.run_ingest_pipeline,
                args=("job-one", Path("/tmp/one.wav")),
            )
            second = threading.Thread(
                target=app_main.run_ingest_pipeline,
                args=("job-two", Path("/tmp/two.wav")),
            )
            first.start()
            time.sleep(0.01)
            second.start()
            first.join(timeout=2)
            second.join(timeout=2)
        finally:
            app_main.run_ingest_pipeline_unlocked = original_worker
            app_main.active_ingest_job_id = original_active_job_id

        self.assertEqual(calls, ["start:job-one", "end:job-one", "start:job-two", "end:job-two"])


if __name__ == "__main__":
    unittest.main()
