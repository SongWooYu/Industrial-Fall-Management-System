import json
from pathlib import Path
import tempfile
import unittest

from industrial_fall.diagnostic_log import DiagnosticLog


class DiagnosticLogTest(unittest.TestCase):
    def test_records_are_flushed_and_rotated_with_bounded_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "events.jsonl"
            logger = DiagnosticLog(str(target), max_bytes=500, backups=2)
            for i in range(40):
                logger.record({"frame": i, "value": "test" * 20})
            logger.close()
            self.assertIsNone(logger.error)
            self.assertEqual(logger.dropped, 0)
            files = list(Path(directory).glob("events.jsonl*"))
            self.assertEqual(len(files), 3)
            self.assertLess(sum(p.stat().st_size for p in files), 1500)
            self.assertEqual(json.loads(target.read_text().splitlines()[-1])["frame"], 39)

    def test_invalid_record_does_not_kill_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "events.jsonl"
            logger = DiagnosticLog(str(target))
            logger.record({"bad": object()})
            logger.record({"good": 1})
            logger.close()
            self.assertEqual(logger.dropped, 1)
            self.assertEqual(json.loads(target.read_text()), {"good": 1})
