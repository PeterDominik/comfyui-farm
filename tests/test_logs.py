"""JSON logs (log_json): the same lines as the text log, one JSON object each, with the job
and the worker a line is about as fields of their own."""

import json
import logging
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from comfyfarm.cli import setup_logging
from comfyfarm.logs import JsonFormatter


def record(msg, *args, level=logging.INFO, **extra):
    r = logging.LogRecord("comfyfarm", level, __file__, 1, msg, args, None)
    r.__dict__.update(extra)
    return r


class FormatterTests(unittest.TestCase):
    def line(self, *a, **k):
        return json.loads(JsonFormatter(role="manager", host="tor-01").format(record(*a, **k)))

    def test_a_line_names_its_job_and_worker(self):
        d = self.line("job %s -> %s (prompt %s)", "1a2b3c4d", "gpu-a", "9f8e7d6c")
        self.assertEqual({k: d[k] for k in ("level", "logger", "role", "host", "msg")},
                         {"level": "info", "logger": "comfyfarm", "role": "manager", "host": "tor-01",
                          "msg": "job 1a2b3c4d -> gpu-a (prompt 9f8e7d6c)"})
        self.assertEqual(d["job"], "1a2b3c4d")
        self.assertRegex(d["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertNotIn("worker", d)                       # this message doesn't say "agent" or "worker"
        d = self.line("agent %s took job %s (%s)", "wrk59467w-5090", "1a2b3c4d", "wan22_i2i")
        self.assertEqual((d["job"], d["worker"]), ("1a2b3c4d", "wrk59467w-5090"))
        self.assertEqual(self.line("set %s: retrying %d batch(es)", "0badf00d", 2)["job"], "0badf00d")
        self.assertEqual(self.line("worker %s: ComfyUI offline: %s", "gpu-b", "refused", level=logging.WARNING)["worker"],
                         "gpu-b")

    def test_a_caller_can_name_them_itself(self):
        d = self.line("requeued after a lost run", job="1a2b3c4d-full-id", worker="gpu-a")
        self.assertEqual((d["job"], d["worker"]), ("1a2b3c4d", "gpu-a"))

    def test_lines_without_a_job_have_no_job_field(self):
        d = self.line("manager started")
        self.assertNotIn("job", d)
        self.assertNotIn("worker", d)

    def test_an_exception_is_one_line_with_the_traceback_in_a_field(self):
        try:
            raise ValueError("boom ünïcode")
        except ValueError:
            r = record("lease check failed", level=logging.ERROR)
            r.exc_info = sys.exc_info()
        out = JsonFormatter(role="agent", host="h", agent="gpu-a").format(r)
        self.assertEqual(out.count("\n"), 0)
        d = json.loads(out)
        self.assertIn("ValueError: boom ünïcode", d["error"])
        self.assertEqual((d["level"], d["agent"]), ("error", "gpu-a"))


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = logging.getLogger()
        self.before, self.level = self.root.handlers[:], self.root.level
        self.root.handlers[:] = []      # ours to replace: setup_logging closes what it finds

    def tearDown(self):
        for h in self.root.handlers:    # Windows can't delete an open log
            h.close()
        self.root.handlers[:] = self.before
        self.root.setLevel(self.level)
        shutil.rmtree(self.tmp)

    def cfg(self, **k):
        return SimpleNamespace(log_dir="logs", resolve=lambda p: self.tmp / p, **k)

    def test_off_by_default_only_the_text_log(self):
        setup_logging(self.cfg())
        logging.getLogger("comfyfarm").info("job %s queued by %s", "1a2b3c4d", "ann")
        self.assertEqual(sorted(p.name for p in (self.tmp / "logs").iterdir()), ["comfyfarm.log"])

    def test_the_manager_writes_both_when_asked(self):
        setup_logging(self.cfg(log_json=True))
        logging.getLogger("comfyfarm").info("job %s queued by %s", "1a2b3c4d", "ann")
        logging.getLogger("comfyfarm").debug("not at this level")
        text = (self.tmp / "logs/comfyfarm.log").read_text(encoding="utf-8")
        lines = [json.loads(ln) for ln in (self.tmp / "logs/comfyfarm.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertIn("comfyfarm: job 1a2b3c4d queued by ann", text)
        self.assertEqual([(d["role"], d["job"], d["msg"]) for d in lines],
                         [("manager", "1a2b3c4d", "job 1a2b3c4d queued by ann")])
        self.assertTrue(lines[0]["host"])
        self.assertNotIn("agent", lines[0])

    def test_an_agent_writes_its_own_file_with_its_name_and_a_second_setup_closes_the_first(self):
        setup_logging(self.cfg(log_json=True))
        first = self.root.handlers[:]
        setup_logging(self.cfg(log_json=True, name="gpu-a"), filename="agent-gpu-a.log")
        self.assertTrue(all(h.stream is None or h.stream.closed for h in first if hasattr(h, "baseFilename")))
        logging.getLogger("comfyfarm").warning("lost event connection to %s", "gpu-a")
        d = json.loads((self.tmp / "logs/agent-gpu-a.jsonl").read_text(encoding="utf-8"))
        self.assertEqual((d["role"], d["agent"], d["level"]), ("agent", "gpu-a", "warning"))


if __name__ == "__main__":
    unittest.main()
