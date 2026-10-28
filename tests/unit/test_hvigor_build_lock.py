"""The compile plugin and the device session must never run hvigor in parallel.

The engine runs hard checks in a thread pool; on dynamic iterations both builds start
at the same moment in the same project. Without the shared lock they race on build/
and .hvigor/ and one fails at random.
"""

import subprocess
import threading
import time

from ecat.entropy.plugins.a2h_verify import _device_session as ds
from ecat.entropy.plugins.a2h_verify import check_compile as cc


class _Recorder:
    def __init__(self) -> None:
        self.spans: list[tuple[str, float, float]] = []
        self._mu = threading.Lock()

    def fake_run(self, cmd, *args, **kwargs):
        who = "device" if "--no-daemon" in cmd and kwargs.get("encoding") else "compile"
        start = time.monotonic()
        time.sleep(0.3)
        end = time.monotonic()
        with self._mu:
            self.spans.append((who, start, end))
        return subprocess.CompletedProcess(cmd, 0, stdout="BUILD SUCCESSFUL", stderr="")


def test_compile_and_device_builds_do_not_overlap(tmp_path, monkeypatch):
    proj = tmp_path / "harmony"
    proj.mkdir()
    hv = tmp_path / "hvigorw"
    hv.write_text("#!/bin/sh\n")

    rec = _Recorder()
    monkeypatch.setattr(subprocess, "run", rec.fake_run)
    monkeypatch.setattr(ds.sys, "platform", "darwin")
    monkeypatch.setattr(cc, "_find_hvigorw", lambda *_a, **_k: hv)
    monkeypatch.setattr(cc, "_build_env", lambda _hv: {})
    monkeypatch.setattr(ds, "_resolve_hvigorw", lambda *_a: hv)
    monkeypatch.setattr(ds, "_resolve_deveco_sdk", lambda _p: str(tmp_path))
    monkeypatch.setattr(ds, "_resolve_deveco_root", lambda _p: None)

    def compile_side():
        cc.CompilePlugin().evaluate(proj, config={})

    def device_side():
        # No .hap is produced by the fake build, so this returns False after building.
        ds._build_and_install_hmos(proj, {"hdc_exe": "hdc", "hmos_target": "t"}, None)

    threads = [threading.Thread(target=compile_side), threading.Thread(target=device_side)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(w for w, _, _ in rec.spans) == ["compile", "device"]
    (_, s1, e1), (_, s2, e2) = sorted(rec.spans, key=lambda s: s[1])
    assert e1 <= s2, f"builds overlapped: first ended {e1:.3f}, second started {s2:.3f}"


def test_both_modules_share_one_lock():
    assert cc.HVIGOR_BUILD_LOCK is ds.HVIGOR_BUILD_LOCK
