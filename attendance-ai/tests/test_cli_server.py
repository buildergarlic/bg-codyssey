import base64
import contextlib
import csv
import io
import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from attendance_ai.cli import main
from attendance_ai.outputs import FILES
from attendance_ai.server import make_server
from attendance_ai.synth import generate, write_export_csv


def _sample(d: Path) -> Path:
    punches, truth = generate(n_employees=12, days=14, seed=3)
    p = d / "출입기록.csv"
    write_export_csv(punches, truth, p)
    return p


class CliTest(unittest.TestCase):
    def test_analyze_then_feed_back_confirmations(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = _sample(tmp)
            out = tmp / "out"
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                self.assertEqual(main(["analyze", str(src), "-o", str(out)]), 0)
            self.assertIn("AI 근태 판정 결과", buf.getvalue())
            for name in FILES:
                self.assertTrue((out / name).exists(), name)
            with open(out / "review_queue.csv", encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            self.assertIn("확정", rows[0])
            # 담당자가 검토 목록의 첫 번째 '판정 불확실/정정 제안' 항목을 AI 제안대로 확정했다고 가정
            target = next((r for r in rows if r["유형"] in ("판정 불확실", "정정 제안")), None)
            if target is None:
                self.skipTest("확정할 판정 항목 없음")
            target["확정"] = target["AI제안"]
            target["메모"] = "담당자 확인"
            confirm = tmp / "검토완료.csv"
            with open(confirm, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
            out2 = tmp / "out2"
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                self.assertEqual(main(["analyze", str(src), "--confirm", str(confirm), "-o", str(out2)]), 0)
            self.assertIn("[확정]", buf.getvalue())
            summary = json.loads((out2 / "model_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["stats"]["by_action"].get("담당자확정"), 1)

    def test_demo(self):
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                self.assertEqual(main(["demo", "-o", tmp, "--employees", "8", "--days", "10"]), 0)
            self.assertIn("정답과 비교한 성능", buf.getvalue())


class ServerTest(unittest.TestCase):
    def test_upload_and_download(self):
        httpd = make_server("127.0.0.1", 0)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                src = _sample(Path(tmp))
                body = json.dumps({"file": {"name": src.name, "b64": base64.b64encode(src.read_bytes()).decode()},
                                   "settings": {"auto_correct_threshold": "0.97"}}).encode()
            base = f"http://127.0.0.1:{port}"
            page = urllib.request.urlopen(base + "/").read().decode()
            self.assertIn("AI 근태 판정기", page)
            req = urllib.request.Request(base + "/api/analyze", data=body, headers={"Content-Type": "application/json"})
            res = json.loads(urllib.request.urlopen(req).read())
            self.assertIn("AI 근태 판정 결과", res["summary"])
            report = urllib.request.urlopen(f"{base}/jobs/{res['job']}/report.html").read().decode()
            self.assertIn("AI 근태 판정 보고서", report)
            self.assertIn("97%", report)
            csv_bytes = urllib.request.urlopen(f"{base}/jobs/{res['job']}/daily_attendance.csv").read()
            self.assertTrue(csv_bytes.startswith("﻿사번".encode("utf-8")))
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(f"{base}/jobs/{res['job']}/../../etc/passwd")
            bad = urllib.request.Request(base + "/api/analyze", data=json.dumps(
                {"file": {"name": "x.csv", "b64": base64.b64encode("아무거나\n1,2\n".encode()).decode()}}).encode())
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(bad)
            self.assertEqual(cm.exception.code, 400)
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
