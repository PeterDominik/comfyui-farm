"""Logs a machine can read: one JSON object per line, next to the text log people read.

The text log stays as it is. With `log_json: true` the same lines also go to a `.jsonl`
file, each with the fields a log collector filters on: when, level, which process on which
machine, and the job and worker the line is about. Standard library only.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import time

# Every log call in the farm names its job as "job 1a2b3c4d" (or set, or cancel) and its
# machine as "agent NAME" or "worker NAME". Reading them back out of the message keeps the
# call sites as they are; a call can also pass them itself: log.info(..., extra={"job": id}).
_JOB = re.compile(r"\b(?:job|set|cancel) ([0-9a-f]{8})\b")
_WORKER = re.compile(r"\b(?:agent|worker) ([\w.-]*\w)")


class JsonFormatter(logging.Formatter):
    def __init__(self, **fixed: str):
        """`fixed` goes on every line: role (manager or agent), host, the agent's name."""
        super().__init__()
        self.fixed = {k: v for k, v in fixed.items() if v}

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
                "level": record.levelname.lower(), "logger": record.name, **self.fixed, "msg": msg}
        job = getattr(record, "job", None) or (m.group(1) if (m := _JOB.search(msg)) else None)
        worker = getattr(record, "worker", None) or (m.group(1) if (m := _WORKER.search(msg)) else None)
        if job:
            line["job"] = str(job)[:8]
        if worker:
            line["worker"] = worker
        if record.exc_info:
            line["error"] = self.formatException(record.exc_info)
        return json.dumps(line, ensure_ascii=False, default=str)


def json_handler(path, role: str, agent: str | None = None) -> logging.Handler:
    """A rotating .jsonl file, sized like the text log."""
    import logging.handlers
    h = logging.handlers.RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    h.setFormatter(JsonFormatter(role=role, host=socket.gethostname(), agent=agent))
    return h
