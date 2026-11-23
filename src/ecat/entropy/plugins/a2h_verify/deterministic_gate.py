"""Bridge between the a2h_verify plugin and the deterministic UI gate (tools/visual_audit.py).

The plugin already captures, per page, `{page}_android.png`, `{page}_harmony.png`,
`{page}_android.json` (uiautomator XML) and `{page}_harmony.json` (uitest dumpLayout).
That is exactly the input the audit needs, so the gate runs on the same evidence the judge
sees — no second capture, no second navigation.

Design rules:
  * never raise: a broken gate must not block the migration loop (report `unavailable`)
  * never guess: if a page pair is missing a dump or a screenshot, that page is `skipped`
  * the gate's own pass/fail comes from tools/visual_audit.py; this module only aggregates
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

AUDIT_TIMEOUT_S = 600


def find_audit_script(start: Path | None = None) -> Path | None:
    """Locate tools/visual_audit.py by walking up from the plugin package."""
    here = (start or Path(__file__).resolve()).resolve()
    for base in [here, *here.parents]:
        cand = base / "tools" / "visual_audit.py"
        if cand.is_file():
            return cand
    return None


def run_page(audit: Path, shots_dir: Path, page: str, bundle: str,
             out_json: Path | None = None) -> dict | None:
    a_png = shots_dir / f"{page}_android.png"
    h_png = shots_dir / f"{page}_harmony.png"
    a_dump = shots_dir / f"{page}_android.json"
    h_dump = shots_dir / f"{page}_harmony.json"
    missing = [p.name for p in (a_png, h_png, a_dump, h_dump) if not p.is_file()]
    if missing:
        return {"page": page, "status": "skipped",
                "reason": f"missing capture(s): {', '.join(missing)}"}

    cmd = [sys.executable, str(audit),
           "--android-xml", str(a_dump), "--android-png", str(a_png),
           "--hmos-json", str(h_dump), "--hmos-png", str(h_png),
           "--bundle", bundle]
    if out_json:
        cmd += ["--json", str(out_json)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=AUDIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"page": page, "status": "unavailable", "reason": "audit timed out"}
    except Exception as exc:                      # noqa: BLE001 - never block the loop
        return {"page": page, "status": "unavailable",
                "reason": f"{type(exc).__name__}: {exc}"}

    payload = None
    if out_json and out_json.is_file():
        try:
            payload = json.loads(out_json.read_text(encoding="utf-8"))
        except Exception:
            payload = None
    if payload is None:
        return {"page": page, "status": "unavailable",
                "reason": (r.stderr or r.stdout or "no output")[-400:]}

    gate = payload.get("gate") or {}
    return {"page": page, "status": "ok",
            "gate": gate.get("verdict", "unknown"),
            "score": gate.get("score", 0.0),
            "counts": payload.get("counts", {}),
            "blocking": gate.get("blocking", []),
            "warnings": gate.get("warnings", []),
            "pairs": payload.get("pairs")}


def run_gate(shots_dir: Path, pages: list[str], bundle: str,
             audit: Path | None = None, out_dir: Path | None = None) -> dict:
    """Run the deterministic gate over every captured page pair and aggregate.

    Returns {"gate": "pass"|"fail"|"unavailable", "score": 0..1, "pages": [...]}.
    A page-level failure dominates; pages that could not be judged are listed but do not
    change the verdict (the judge still scores them).
    """
    audit = audit or find_audit_script()
    if audit is None:
        return {"gate": "unavailable", "score": 0.0, "pages": [],
                "reason": "tools/visual_audit.py not found above the plugin package"}

    results = []
    for page in pages:
        out_json = (out_dir / f"{page}_audit.json") if out_dir else None
        if out_json:
            out_json.parent.mkdir(parents=True, exist_ok=True)
        results.append(run_page(audit, shots_dir, page, bundle, out_json) or
                       {"page": page, "status": "unavailable"})

    judged = [r for r in results if r.get("status") == "ok"]
    failing = [r for r in judged if r.get("gate") == "fail"]
    score = max([float(r.get("score") or 0.0) for r in failing], default=0.0)
    return {
        "gate": "fail" if failing else ("pass" if judged else "unavailable"),
        "score": round(score, 3),
        "judged": len(judged),
        "requested": len(results),
        "failing_pages": [r["page"] for r in failing],
        "pages": results,
    }


def blocking_issues(gate: dict, limit: int = 8) -> list[str]:
    """Flatten the gate's blocking rows into human lines a WorkItem can carry."""
    out: list[str] = []
    for page in gate.get("pages") or []:
        if page.get("status") != "ok":
            continue
        for row in (page.get("blocking") or [])[:limit]:
            what = row.get("text") or row.get("sample") or ""
            detail = row.get("gate_reason") or row.get("verdict")
            out.append(f"ui_gate[{page['page']}]: {row.get('verdict')} "
                       f"{row.get('role') or ''} {what} — {detail}".strip())
    return out[:limit]
