import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common.role_definitions.access_log import RDAccessLog


class RDAccessLogTests(unittest.TestCase):
    def test_concurrent_instances_append_complete_utf8_json_lines(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "logs" / "rd_access.jsonl"
            logs = [RDAccessLog(path) for _ in range(8)]

            def write_record(sequence: int) -> None:
                logs[sequence % len(logs)].record(
                    "定義アクセス",
                    sequence=sequence,
                    message="日本語を含む監査レコード",
                )

            with ThreadPoolExecutor(max_workers=len(logs)) as executor:
                list(executor.map(write_record, range(400)))

            lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
            records = [json.loads(line) for line in lines]

            self.assertEqual(len(records), 400)
            self.assertEqual({record["sequence"] for record in records}, set(range(400)))
            self.assertTrue(all(record["event"] == "定義アクセス" for record in records))
            self.assertTrue(
                all(record["message"] == "日本語を含む監査レコード" for record in records)
            )


if __name__ == "__main__":
    unittest.main()
