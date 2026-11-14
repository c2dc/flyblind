##################################################################################################################################
# Module: test_terminal_report.py (unit tests — terminal_report)                                                                 #
# Part of the paper: FlyBlind: Silent State Staleness in UAS Command and Control over 5G                                         #
#                                                                                                                                #
# Author: (anonymous; double-blind submission).                                                                                  #
# Institution: withheld (double-blind review).                                                                                   #
# Date: 2026-11                                                                                                                  #
# License: MIT License                                                                                                           #
#                                                                                                                                #
# Description: Terminal formatting and gate display tests.                                                                       #
##################################################################################################################################

import io
import sys
import tempfile
import unittest
from pathlib import Path

_LIB = Path(__file__).resolve().parents[1] / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import terminal_report as report


class TestCampaignTranscript(unittest.TestCase):
    def test_mirrors_stdout_stderr_and_uses_campaign_name(self):
        with tempfile.TemporaryDirectory() as td:
            campaign = Path(td) / "106_PRB-CROSS-BASELINE-60S"
            original_stdout, original_stderr = sys.stdout, sys.stderr
            visible_stdout, visible_stderr = io.StringIO(), io.StringIO()
            sys.stdout, sys.stderr = visible_stdout, visible_stderr
            transcript = None
            try:
                transcript = report.start_campaign_transcript(campaign)
                print("stdout line")
                print("stderr line", file=sys.stderr)
                transcript.close()
            finally:
                if transcript is not None:
                    transcript.close()
                sys.stdout, sys.stderr = original_stdout, original_stderr

            self.assertEqual(transcript.path.name, "106_PRB-CROSS-BASELINE-60S.txt")
            saved = transcript.path.read_text(encoding="utf-8")
            self.assertIn("stdout line", saved)
            self.assertIn("stderr line", saved)
            self.assertIn("stdout line", visible_stdout.getvalue())
            self.assertIn("stderr line", visible_stderr.getvalue())

    def test_existing_name_gets_alphabetical_suffix(self):
        with tempfile.TemporaryDirectory() as td:
            campaign = Path(td) / "CAMPAIGN"
            campaign.mkdir()
            (campaign / "CAMPAIGN.txt").write_text("anterior\n", encoding="utf-8")

            transcript = report.start_campaign_transcript(campaign)
            try:
                self.assertEqual(transcript.path.name, "CAMPAIGN_a.txt")
            finally:
                transcript.close()


if __name__ == "__main__":
    unittest.main()
