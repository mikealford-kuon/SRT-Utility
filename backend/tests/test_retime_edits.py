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

    def test_legacy_dialog_question_answer_gets_newline_inside_same_timing(self) -> None:
        old_segments = [
            segment(
                "old-1",
                0.0,
                8.0,
                "How do you open a work package? I meet with Amy and the program team.",
            ),
        ]
        new_segments = [
            segment(
                "new-1",
                20.0,
                28.0,
                "How do you open a work package I meet with Amy and the program team",
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
            "How do you open a work package?\nI meet with Amy and the program team.",
        )
        self.assertEqual(retimed[0].start_seconds, 20.0)
        self.assertEqual(report.matched_segments, 1)

    def test_legacy_dialog_sentence_runon_gets_punctuation_newline(self) -> None:
        old_segments = [
            segment(
                "old-1",
                0.0,
                8.0,
                "Yes, and no. The hours will match exactly.",
            ),
        ]
        new_segments = [
            segment(
                "new-1",
                30.0,
                38.0,
                "Yes and no the hours will match exactly",
            ),
        ]

        retimed, _ = retime_edited_subtitle_segments(
            old_segments=old_segments,
            new_timing_segments=new_segments,
            source_file_name="previous.vtt",
            source_format="vtt",
            threshold=0.58,
        )

        self.assertEqual(retimed[0].text, "Yes, and no.\nThe hours will match exactly.")

    def test_dialog_line_break_cleanup_does_not_split_common_titles(self) -> None:
        self.assertEqual(
            app_main.normalize_dialog_turn_line_breaks("Mr. Smith answers. Amy continues."),
            "Mr. Smith answers.\nAmy continues.",
        )

    def test_changed_mp4_insert_delete_shape_wins_inside_matched_segment(self) -> None:
        old_segments = [
            segment(
                "old-1",
                0.0,
                8.0,
                "Welcome to the EVMS course. This removed sentence should not return.",
            ),
        ]
        new_segments = [
            segment(
                "new-1",
                20.0,
                23.5,
                "welcome to the evms course this new MP4 sentence was inserted",
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
            "Welcome to the EVMS course.\nThis new MP4 sentence was inserted",
        )
        self.assertEqual(retimed[0].start_seconds, 20.0)
        self.assertEqual(retimed[0].end_seconds, 23.5)
        self.assertNotIn("removed sentence", retimed[0].text)
        self.assertEqual(report.matched_segments, 1)

    def test_matched_legacy_punctuation_is_not_overwritten_by_global_corrections(self) -> None:
        old_segments = [
            segment(
                "old-1",
                0.0,
                4.0,
                "These QBDs are weighted using the resources required to complete the effort.",
            ),
            segment(
                "old-2",
                4.0,
                8.0,
                "The percent complete EVT requires QBD's tracking records.",
            ),
        ]
        new_segments = [
            segment(
                "new-1",
                10.0,
                14.0,
                "These QBDs are weighted using the resources required to complete the effort.",
            ),
            segment(
                "new-2",
                14.0,
                18.0,
                "The percent complete EVT requires QBDs tracking records.",
            ),
        ]

        retimed, report = retime_edited_subtitle_segments(
            old_segments=old_segments,
            new_timing_segments=new_segments,
            source_file_name="previous.vtt",
            source_format="vtt",
            threshold=0.58,
        )

        self.assertEqual(retimed[0].text, old_segments[0].text)
        self.assertEqual(retimed[1].text, old_segments[1].text)
        self.assertEqual(report.applied_corrections, 0)
        self.assertTrue(
            any(
                suggestion.wrong_text == "QBDs"
                and suggestion.corrected_text == "QBD's"
                for suggestion in report.learned_corrections
            )
        )

    def test_matched_legacy_commas_are_preserved_with_mp4_timing(self) -> None:
        old_segments = [
            segment(
                "old-1",
                0.0,
                6.0,
                "Because of the weighting, I will take 50% when the team starts, and the remaining 50% when we finish the work package.",
            ),
        ]
        new_segments = [
            segment(
                "new-1",
                20.0,
                25.0,
                "Because of the weighting I will take 50% when the team starts and the remaining 50% when",
            ),
            segment("new-2", 25.0, 28.0, "we finish the work package."),
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
            "Because of the weighting, I will take 50% when the team starts, and the remaining 50% when",
        )
        self.assertEqual(retimed[1].text, "we finish the work package.")
        self.assertEqual(retimed[0].start_seconds, 20.0)
        self.assertEqual(retimed[1].end_seconds, 28.0)
        self.assertEqual(report.matched_segments, 2)

    def test_legacy_punctuation_at_split_boundary_is_preserved(self) -> None:
        old_segments = [
            segment(
                "old-1",
                0.0,
                6.0,
                "The schedule, budget, and scope remain aligned.",
            ),
        ]
        new_segments = [
            segment("new-1", 20.0, 22.0, "The schedule"),
            segment("new-2", 22.0, 25.0, "budget and scope remain aligned"),
        ]

        retimed, report = retime_edited_subtitle_segments(
            old_segments=old_segments,
            new_timing_segments=new_segments,
            source_file_name="previous.vtt",
            source_format="vtt",
            threshold=0.58,
        )

        self.assertEqual(retimed[0].text, "The schedule,")
        self.assertEqual(retimed[1].text, "budget, and scope remain aligned.")
        self.assertEqual(retimed[0].start_seconds, 20.0)
        self.assertEqual(retimed[1].end_seconds, 25.0)
        self.assertEqual(report.matched_segments, 2)

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

    def test_stale_legacy_end_does_not_override_current_mp4_end(self) -> None:
        old_segments = [
            segment(
                "old-1",
                8.0,
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
        self.assertEqual(retimed[0].end_seconds, 15.0)
        self.assertNotIn("extended", retimed[0].retime_note or "")
        self.assertEqual(report.matched_segments, 2)

    def test_zero_start_first_cue_clamps_to_detected_leading_speech(self) -> None:
        segments = [
            segment("new-1", 0.0, 13.56, "This segment starts after the video intro."),
            segment("new-2", 13.56, 16.52, "The second timing already lines up."),
        ]

        adjusted, did_adjust = app_main.clamp_first_segment_to_detected_speech(
            segments,
            5.832,
        )

        self.assertTrue(did_adjust)
        self.assertEqual(adjusted[0].start_seconds, 5.832)
        self.assertEqual(adjusted[0].end_seconds, 13.56)
        self.assertEqual(adjusted[1].start_seconds, 13.56)
        self.assertIn("leading silence", adjusted[0].retime_note or "")

    def test_leading_speech_clamp_leaves_existing_nonzero_start_alone(self) -> None:
        segments = [
            segment("new-1", 4.92, 13.56, "This segment already starts near speech."),
        ]

        adjusted, did_adjust = app_main.clamp_first_segment_to_detected_speech(
            segments,
            5.832,
        )

        self.assertFalse(did_adjust)
        self.assertEqual(adjusted[0].start_seconds, 4.92)

    def test_dense_current_mp4_caption_gap_beats_stale_or_long_vtt_end(self) -> None:
        old_segments = [
            segment(
                "old-1",
                52.452,
                67.474,
                "of $1,621,825, against my BAC of $1,66750, That's slightly less than a 1% variance at completion.",
            ),
            segment(
                "old-2",
                69.228,
                76.454,
                "As I recall, when you showed me your updated control account plan, you were only about 13% complete.",
            ),
        ]
        new_segments = [
            segment(
                "new-1",
                52.452,
                55.474,
                "Of $1,621,825, against my BAC of $1,66750, that's slightly less than a 1% variance at completion.",
            ),
            segment(
                "new-2",
                69.228,
                76.454,
                "As I recall, when you showed me your updated control account plan, you were only about 13% complete.",
            ),
        ]

        retimed, report = retime_edited_subtitle_segments(
            old_segments=old_segments,
            new_timing_segments=new_segments,
            source_file_name="previous.vtt",
            source_format="vtt",
            threshold=0.58,
        )

        self.assertEqual(retimed[0].start_seconds, 52.452)
        self.assertAlmostEqual(retimed[0].end_seconds, 67.728)
        self.assertIn("Dense caption timing was extended", retimed[0].retime_note or "")
        self.assertLess(retimed[0].end_seconds, retimed[1].start_seconds)
        self.assertEqual(report.matched_segments, 2)

    def test_dense_current_mp4_caption_gap_repairs_stale_short_vtt_end(self) -> None:
        old_segments = [
            segment(
                "old-1",
                52.452,
                55.474,
                "That's slightly less than a 1% variance at completion.",
            ),
            segment(
                "old-2",
                69.228,
                77.123,
                "As I recall, when you showed me your updated control account plan, you were only about 13% complete.",
            ),
        ]
        new_segments = [
            segment(
                "new-1",
                52.452,
                55.474,
                "Of $1,621,825, against my BAC of $1,66750, that's slightly less than a 1% variance at completion.",
            ),
            segment(
                "new-2",
                69.228,
                77.123,
                "As I recall, when you showed me your updated control account plan, you were only about 13% complete.",
            ),
        ]

        retimed, report = retime_edited_subtitle_segments(
            old_segments=old_segments,
            new_timing_segments=new_segments,
            source_file_name="previous.vtt",
            source_format="vtt",
            threshold=0.58,
        )

        self.assertEqual(retimed[0].start_seconds, 52.452)
        self.assertAlmostEqual(retimed[0].end_seconds, 67.728)
        self.assertIn("Dense caption timing was extended", retimed[0].retime_note or "")
        self.assertLess(retimed[0].end_seconds, retimed[1].start_seconds)
        self.assertGreaterEqual(report.matched_segments, 1)

    def test_legacy_end_timing_does_not_override_current_mp4_segment(self) -> None:
        old_segments = [
            segment("old-1", 8.5, 20.0, "This cue should not overlap the next one."),
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

        self.assertAlmostEqual(retimed[0].end_seconds, 15.0)
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
        self.assertEqual(retimed[1].end_seconds, 36.0)
        self.assertNotIn("extended", retimed[1].retime_note or "")
        self.assertEqual(retimed[2].retime_status, "new-only")
        self.assertEqual(report.matched_segments, 2)

    def test_prefix_only_current_text_preserves_uploaded_vtt_tail(self) -> None:
        old_segments = [
            segment("old-1", 25.4, 26.4, "Yes. That's right, Noah."),
            segment("old-2", 27.0, 30.0, "The next complete caption is unchanged."),
        ]
        new_segments = [
            segment("new-1", 25.45, 25.75, "Yes."),
            segment("new-2", 27.1, 29.8, "The next complete caption is unchanged."),
        ]

        retimed, report = retime_edited_subtitle_segments(
            old_segments=old_segments,
            new_timing_segments=new_segments,
            source_file_name="previous.vtt",
            source_format="vtt",
            threshold=0.58,
        )

        self.assertEqual(retimed[0].text, "Yes.")
        self.assertEqual(retimed[0].retime_status, "matched")
        self.assertIn("VTT-only text was removed", retimed[0].retime_note or "")
        self.assertEqual(retimed[0].end_seconds, 25.75)
        self.assertEqual(report.matched_segments, 2)
        self.assertEqual(report.low_confidence_segments, 0)
        self.assertEqual(report.unmatched_old_segments, 0)

    def test_prefix_only_current_text_preserves_longer_uploaded_vtt_tail(self) -> None:
        old_segments = [
            segment(
                "old-1",
                40.0,
                47.0,
                "Kate, are you okay or should we do the remaining five work packages?",
            ),
        ]
        new_segments = [
            segment("new-1", 40.2, 42.0, "Kate, are you okay"),
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
            "Kate, are you okay",
        )
        self.assertEqual(retimed[0].retime_status, "matched")
        self.assertIn("VTT-only text was removed", retimed[0].retime_note or "")
        self.assertEqual(report.matched_segments, 1)
        self.assertEqual(report.low_confidence_segments, 0)

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

    def test_tiny_trailing_split_fragment_stays_attached_to_previous_cue(self) -> None:
        old_segments = [
            segment("old-1", 92.087, 95.788, "Lucas, what do you mean by an LOE, EVT?"),
            segment("old-2", 97.228, 100.749, "LOE is one of the seven EVTs that our system allows us to use."),
        ]
        new_segments = [
            segment("new-1", 92.087, 94.528, "Lucas, what do you mean by an LOE"),
            segment("new-2", 95.248, 95.788, "EVT."),
            segment("new-3", 97.228, 100.749, "LOE is one of the seven EVTs that our system allows us to use."),
        ]

        retimed, report = retime_edited_subtitle_segments(
            old_segments=old_segments,
            new_timing_segments=new_segments,
            source_file_name="previous.vtt",
            source_format="vtt",
            threshold=0.58,
        )

        self.assertEqual(len(retimed), 2)
        self.assertEqual(retimed[0].text, "Lucas, what do you mean by an LOE, EVT?")
        self.assertAlmostEqual(retimed[0].end_seconds, 95.788)
        self.assertEqual(retimed[1].text, "LOE is one of the seven EVTs that our system allows us to use.")
        self.assertEqual(report.matched_segments, 2)
        self.assertEqual(report.unmatched_new_segments, 0)

    def test_low_confidence_subsequence_projects_vtt_style_onto_current_mp4_text(self) -> None:
        # Use small, short cues so the alignment score falls below threshold but
        # the new timing text is still a clear prefix of the old one.
        old_segments = [
            segment("old-1", 10.0, 14.5, "Welcome to our evms training course for control account managers"),
        ]
        new_segments = [
            segment("new-1", 10.05, 11.5, "Welcome to our evms training"),
        ]

        retimed, report = retime_edited_subtitle_segments(
            old_segments=old_segments,
            new_timing_segments=new_segments,
            source_file_name="previous.vtt",
            source_format="vtt",
            threshold=0.95,  # push threshold high to force the low-confidence path
        )

        self.assertEqual(retimed[0].text, "Welcome to our evms training")
        self.assertEqual(retimed[0].end_seconds, 11.5)
        self.assertIn("Current MP4", retimed[0].retime_note or "")
        # Confidence should match the underlying score, not be artificially bumped
        # above the high threshold (we trust the wording but not the score).
        self.assertGreaterEqual(report.matched_segments, 1)

    def test_low_confidence_unrelated_vtt_does_not_overwrite_new_timing(self) -> None:
        # When the previously uploaded VTT is textually unrelated, do not preserve
        # its old text - the placeholder / re-transcribed text is closer to truth.
        old_segments = [
            segment("old-1", 0.0, 3.0, "A completely different sentence about a different topic."),
        ]
        new_segments = [
            segment("new-1", 0.0, 2.23, "Ready-to-export subtitle line 1."),
        ]

        retimed, _ = retime_edited_subtitle_segments(
            old_segments=old_segments,
            new_timing_segments=new_segments,
            source_file_name="previous.vtt",
            source_format="vtt",
            threshold=0.58,
        )

        self.assertEqual(retimed[0].text, "Ready-to-export subtitle line 1.")
        self.assertEqual(retimed[0].retime_status, "low-confidence")

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

    def test_endpoint_removes_uploaded_vtt_tail_when_current_mp4_text_is_prefix_only(self) -> None:
        app_main.jobs[0] = app_main.jobs[0].model_copy(
            update={
                "transcript_segments": [
                    segment("new-1", 25.45, 25.75, "Yes."),
                    segment("new-2", 27.1, 29.8, "The next complete caption is unchanged."),
                ],
            }
        )
        previous_vtt = (
            "WEBVTT\n\n"
            "00:00:25.400 --> 00:00:26.400\n"
            "Yes. That's right, Noah.\n\n"
            "00:00:27.000 --> 00:00:30.000\n"
            "The next complete caption is unchanged.\n"
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
        self.assertEqual(payload["transcript_segments"][0]["text"], "Yes.")
        self.assertEqual(payload["transcript_segments"][0]["end_seconds"], 25.75)
        self.assertIn("VTT-only text was removed", payload["transcript_segments"][0]["retime_note"])
        self.assertEqual(payload["retime_report"]["matched_segments"], 2)
        self.assertEqual(payload["retime_report"]["low_confidence_segments"], 0)

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
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_jobs = list(app_main.jobs)
        self.original_data_dir = app_main.DATA_DIR
        self.original_uploads_dir = app_main.UPLOADS_DIR
        self.original_tracks_dir = app_main.TRACKS_DIR
        self.original_artifacts_dir = app_main.ARTIFACTS_DIR
        self.original_store_path = app_main.STORE_PATH
        self.original_next_job_id = app_main.next_job_id
        app_main.DATA_DIR = Path(self.temp_dir.name)
        app_main.UPLOADS_DIR = app_main.DATA_DIR / "uploads"
        app_main.TRACKS_DIR = app_main.DATA_DIR / "tracks"
        app_main.ARTIFACTS_DIR = app_main.DATA_DIR / "artifacts"
        app_main.STORE_PATH = app_main.DATA_DIR / "jobs.json"
        app_main.jobs[:] = []
        app_main.next_job_id = 1

    def tearDown(self) -> None:
        app_main.jobs[:] = self.original_jobs
        app_main.DATA_DIR = self.original_data_dir
        app_main.UPLOADS_DIR = self.original_uploads_dir
        app_main.TRACKS_DIR = self.original_tracks_dir
        app_main.ARTIFACTS_DIR = self.original_artifacts_dir
        app_main.STORE_PATH = self.original_store_path
        app_main.next_job_id = self.original_next_job_id
        self.temp_dir.cleanup()

    def add_queued_ingest_job(self, job_id: str, media_path: Path) -> None:
        now = datetime.now(timezone.utc).isoformat()
        app_main.jobs.append(
            JobDetail(
                job_id=job_id,
                kind="ingest",
                media_path=str(media_path),
                media_metadata=None,
                transcription_mode="placeholder",
                transcription_source="queued-awaiting-processing",
                timing_source="queued-awaiting-processing",
                stage="queued",
                progress_percent=app_main.STAGE_PROGRESS["queued"],
                stage_label=app_main.STAGE_LABELS["queued"],
                stage_description=app_main.STAGE_DESCRIPTIONS["queued"],
                created_at=now,
                updated_at=now,
                transcript_segments=app_main.build_placeholder_segments(
                    stage="queued",
                    job_id=job_id,
                    media_metadata=None,
                ),
            )
        )

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

    def test_ingest_without_real_transcript_fails_without_ready_placeholders(self) -> None:
        media_path = Path(self.temp_dir.name) / "lesson.mp4"
        media_path.write_bytes(b"not-real-video")
        self.add_queued_ingest_job("job-no-transcript", media_path)
        original_build_media_metadata = app_main.build_media_metadata
        original_transcribe = app_main.run_local_transcription_with_timeout

        def fake_build_media_metadata(path: Path) -> app_main.MediaMetadata:
            return app_main.MediaMetadata(
                file_name=path.name,
                size_bytes=path.stat().st_size,
                duration_seconds=71.62,
                has_video=True,
                has_audio=True,
            )

        def fake_transcribe(**_: object) -> tuple[list[TranscriptSegment] | None, str, str, str]:
            return (
                None,
                "placeholder",
                "placeholder-fallback:transcriber-no-output",
                "placeholder-fallback:transcriber-no-output",
            )

        try:
            app_main.build_media_metadata = fake_build_media_metadata
            app_main.run_local_transcription_with_timeout = fake_transcribe
            app_main.run_ingest_pipeline_unlocked("job-no-transcript", media_path)
        finally:
            app_main.build_media_metadata = original_build_media_metadata
            app_main.run_local_transcription_with_timeout = original_transcribe

        job = app_main.jobs[0]
        self.assertEqual(job.stage, "failed")
        self.assertEqual(job.progress_percent, app_main.STAGE_PROGRESS["failed"])
        self.assertEqual(job.transcript_segments, [])
        self.assertIn("transcriber-no-output", job.transcription_source)

    def test_ingest_pipeline_exception_fails_without_ready_placeholders(self) -> None:
        media_path = Path(self.temp_dir.name) / "lesson.mp4"
        media_path.write_bytes(b"not-real-video")
        self.add_queued_ingest_job("job-exception", media_path)
        original_build_media_metadata = app_main.build_media_metadata

        def fake_build_media_metadata(_: Path) -> app_main.MediaMetadata:
            raise RuntimeError("probe failed")

        try:
            app_main.build_media_metadata = fake_build_media_metadata
            app_main.run_ingest_pipeline_unlocked("job-exception", media_path)
        finally:
            app_main.build_media_metadata = original_build_media_metadata

        job = app_main.jobs[0]
        self.assertEqual(job.stage, "failed")
        self.assertEqual(job.transcript_segments, [])
        self.assertIn("pipeline-error:RuntimeError", job.transcription_source)

    def test_pending_legacy_vtt_applies_to_final_ready_job_payload(self) -> None:
        media_path = Path(self.temp_dir.name) / "lesson.mp4"
        media_path.write_bytes(b"not-real-video")
        pending_path = app_main.DATA_DIR / "tracks" / "old-edit.vtt"
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.write_text(
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:01.000\n"
            "Welcome to the EDITED course!\n",
            encoding="utf-8",
        )
        self.add_queued_ingest_job("job-final-retime", media_path)
        app_main.jobs[0] = app_main.jobs[0].model_copy(
            update={
                "pending_legacy_subtitle_path": str(pending_path),
                "pending_legacy_subtitle_name": "old-edit.vtt",
            }
        )

        original_build_media_metadata = app_main.build_media_metadata
        original_transcribe = app_main.run_local_transcription_with_timeout
        original_sleep = app_main.time.sleep

        def fake_build_media_metadata(path: Path) -> app_main.MediaMetadata:
            return app_main.MediaMetadata(
                file_name=path.name,
                size_bytes=path.stat().st_size,
                duration_seconds=7.0,
                has_video=True,
                has_audio=True,
            )

        def fake_transcribe(**_: object) -> tuple[list[TranscriptSegment], str, str, str]:
            return (
                [
                    segment(
                        "job-final-retime-seg-001",
                        5.5,
                        7.25,
                        "welcome to the edited course",
                    )
                ],
                "real-cli",
                "real-cli:unit-test",
                "unit-test-timing",
            )

        try:
            app_main.build_media_metadata = fake_build_media_metadata
            app_main.run_local_transcription_with_timeout = fake_transcribe
            app_main.time.sleep = lambda _: None
            app_main.run_ingest_pipeline_unlocked("job-final-retime", media_path)
        finally:
            app_main.build_media_metadata = original_build_media_metadata
            app_main.run_local_transcription_with_timeout = original_transcribe
            app_main.time.sleep = original_sleep

        job = app_main.jobs[0]
        self.assertEqual(job.stage, "ready")
        self.assertEqual(job.transcript_segments[0].start_seconds, 5.5)
        self.assertEqual(job.transcript_segments[0].end_seconds, 7.25)
        self.assertEqual(job.transcript_segments[0].text, "Welcome to the EDITED course!")
        self.assertIsNone(job.pending_legacy_subtitle_path)
        self.assertIn("retimed-edits:vtt", job.transcription_source)

    def test_ready_job_detail_repairs_zero_start_against_detected_leading_silence(self) -> None:
        media_path = Path(self.temp_dir.name) / "lesson.mp4"
        media_path.write_bytes(b"not-real-video")
        now = datetime.now(timezone.utc).isoformat()
        app_main.jobs[:] = [
            JobDetail(
                job_id="job-leading-silence",
                kind="ingest",
                media_path=str(media_path),
                transcription_mode="real-cli",
                transcription_source="real-cli:whisper",
                timing_source="plain-whisper-cli-srt",
                stage="ready",
                progress_percent=100,
                stage_label="Ready",
                stage_description="Processing complete.",
                created_at=now,
                updated_at=now,
                transcript_segments=[
                    segment("job-leading-silence-seg-001", 0.0, 13.56, "Starts after intro."),
                ],
            )
        ]
        original_detect = app_main.detect_leading_audio_silence_end
        original_which = app_main.shutil.which

        try:
            app_main.detect_leading_audio_silence_end = lambda **_: 5.832
            app_main.shutil.which = lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else original_which(name)
            detail = app_main.get_job("job-leading-silence")
        finally:
            app_main.detect_leading_audio_silence_end = original_detect
            app_main.shutil.which = original_which

        self.assertEqual(detail.transcript_segments[0].start_seconds, 5.832)
        self.assertIn("leading-silence-clamped", detail.timing_source)
        self.assertIn("leading-silence-clamped", app_main.jobs[0].timing_source)

    def test_ready_job_detail_splits_dialog_turns_into_timed_cues(self) -> None:
        media_path = Path(self.temp_dir.name) / "lesson.mp4"
        media_path.write_bytes(b"not-real-video")
        now = datetime.now(timezone.utc).isoformat()
        app_main.jobs[:] = [
            JobDetail(
                job_id="job-dialog-turns",
                kind="ingest",
                media_path=str(media_path),
                transcription_mode="real-cli",
                transcription_source="retimed-edits:vtt",
                timing_source="plain-whisper-cli-srt|retimed-edits:vtt",
                stage="ready",
                progress_percent=100,
                stage_label="Ready",
                stage_description="Processing complete.",
                created_at=now,
                updated_at=now,
                transcript_segments=[
                    segment(
                        "job-dialog-turns-seg-001",
                        10.0,
                        16.0,
                        "How do you open a work package? I meet with Amy.",
                    ),
                ],
            )
        ]

        detail = app_main.get_job("job-dialog-turns")

        self.assertEqual(len(detail.transcript_segments), 2)
        self.assertEqual(detail.transcript_segments[0].text, "How do you open a work package?")
        self.assertEqual(detail.transcript_segments[1].text, "I meet with Amy.")
        self.assertEqual(detail.transcript_segments[0].start_seconds, 10.0)
        self.assertGreater(detail.transcript_segments[1].start_seconds, 10.0)
        self.assertEqual(detail.transcript_segments[1].end_seconds, 16.0)
        self.assertIn("dialog-turn-lines-normalized", detail.timing_source)
        self.assertIn("dialog-turn-timing-split", detail.timing_source)
        self.assertIn("dialog-turn-lines-normalized", app_main.jobs[0].timing_source)

    def test_ready_job_dialog_repair_runs_even_when_old_marker_exists(self) -> None:
        media_path = Path(self.temp_dir.name) / "lesson.mp4"
        media_path.write_bytes(b"not-real-video")
        now = datetime.now(timezone.utc).isoformat()
        app_main.jobs[:] = [
            JobDetail(
                job_id="job-dialog-marker",
                kind="ingest",
                media_path=str(media_path),
                transcription_mode="real-cli",
                transcription_source="retimed-edits:vtt",
                timing_source="plain-whisper-cli-srt|dialog-turn-lines-normalized",
                stage="ready",
                progress_percent=100,
                stage_label="Ready",
                stage_description="Processing complete.",
                created_at=now,
                updated_at=now,
                transcript_segments=[
                    segment(
                        "job-dialog-marker-seg-001",
                        20.0,
                        26.0,
                        "Noah, it's two things.  The interface milestones matter.",
                    ),
                ],
            )
        ]

        detail = app_main.get_job("job-dialog-marker")

        self.assertEqual(
            [item.text for item in detail.transcript_segments],
            ["Noah, it's two things.", "The interface milestones matter."],
        )
        self.assertIn("dialog-turn-lines-normalized", detail.timing_source)
        self.assertIn("dialog-turn-timing-split", detail.timing_source)

    def test_ready_job_dialog_repair_removes_adjacent_duplicate_boundary(self) -> None:
        media_path = Path(self.temp_dir.name) / "lesson.mp4"
        media_path.write_bytes(b"not-real-video")
        now = datetime.now(timezone.utc).isoformat()
        app_main.jobs[:] = [
            JobDetail(
                job_id="job-dialog-duplicate",
                kind="ingest",
                media_path=str(media_path),
                transcription_mode="real-cli",
                transcription_source="retimed-edits:vtt",
                timing_source="plain-whisper-cli-srt|retimed-edits:vtt",
                stage="ready",
                progress_percent=100,
                stage_label="Ready",
                stage_description="Processing complete.",
                created_at=now,
                updated_at=now,
                transcript_segments=[
                    segment(
                        "job-dialog-duplicate-seg-001",
                        10.0,
                        18.0,
                        "Is that all right with you? Okay. Lucas, let's both",
                    ),
                    segment(
                        "job-dialog-duplicate-seg-002",
                        18.0,
                        24.0,
                        "Okay. Lucas, let's both add this to that action item.",
                    ),
                ],
            )
        ]

        detail = app_main.get_job("job-dialog-duplicate")
        combined = " ".join(segment.text for segment in detail.transcript_segments)

        self.assertEqual(combined.count("Lucas, let's both"), 1)
        self.assertIn("Is that all right with you?", combined)
        self.assertIn("Okay.", combined)
        okay_segment = next(
            segment for segment in detail.transcript_segments if segment.text == "Okay."
        )
        self.assertGreaterEqual(
            okay_segment.end_seconds - okay_segment.start_seconds,
            app_main.DIALOG_TURN_MIN_DURATION_SECONDS,
        )

    def test_manual_advance_cannot_turn_placeholder_transcript_into_ready_output(self) -> None:
        media_path = Path(self.temp_dir.name) / "lesson.mp4"
        media_path.write_bytes(b"not-real-video")
        self.add_queued_ingest_job("job-manual-advance", media_path)

        for _ in range(6):
            app_main.advance_job("job-manual-advance")

        job = app_main.jobs[0]
        self.assertEqual(job.stage, "failed")
        self.assertEqual(job.transcript_segments, [])

    def test_transcription_timeout_scales_with_media_duration(self) -> None:
        timeout_seconds = app_main.resolve_transcription_timeout_seconds(
            app_main.MediaMetadata(
                file_name="lesson.mp4",
                size_bytes=518_539_780,
                duration_seconds=579.264,
                has_video=True,
                has_audio=True,
            )
        )

        self.assertGreaterEqual(timeout_seconds, 1448)


if __name__ == "__main__":
    unittest.main()
