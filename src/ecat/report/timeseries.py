"""EntropyTimeseries — append-only JSONL log of entropy over iterations."""

from __future__ import annotations

import json
from pathlib import Path

from ecat.entropy.base import EntropyReport


class EntropyTimeseries:
    """Manages the entropy_history.jsonl file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("", encoding="utf-8")

    def append(self, report: EntropyReport) -> None:
        line = json.dumps(report.to_dict())
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def load(self) -> list[EntropyReport]:
        records = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                records.append(_dict_to_report(data))
            except (json.JSONDecodeError, KeyError):
                pass
        return records


def _dict_to_report(data: dict[str, object]) -> EntropyReport:
    from datetime import datetime

    ts = data.get("timestamp")
    timestamp = datetime.fromisoformat(str(ts)) if ts else None
    raw_bd = data.get("breakdown", {})
    raw_dt = data.get("details", {})
    breakdown: dict[str, float] = {str(k): float(v) for k, v in (raw_bd if isinstance(raw_bd, dict) else {}).items()}
    details: dict[str, dict[str, object]] = {str(k): (v if isinstance(v, dict) else {}) for k, v in (raw_dt if isinstance(raw_dt, dict) else {}).items()}
    report = EntropyReport(
        iteration=int(str(data.get("iteration", 0))),
        total_H=float(str(data.get("total_H", 1.0))),
        breakdown=breakdown,
        details=details,
    )
    if timestamp is not None:
        report.timestamp = timestamp
    return report
