"""Cross-platform build/install helpers of the device session (runtime_liveness + ui_align)."""

from pathlib import Path

import pytest

from ecat.entropy.plugins.a2h_verify import _device_session as ds


def _fake_deveco(root: Path) -> Path:
    (root / "tools" / "hvigor" / "bin").mkdir(parents=True)
    (root / "tools" / "hvigor" / "bin" / "hvigorw").write_text("#!/bin/sh\n")
    (root / "tools" / "hvigor" / "bin" / "hvigorw.bat").write_text("@echo off\n")
    (root / "tools" / "node" / "bin").mkdir(parents=True)
    (root / "jbr" / "Contents" / "Home" / "bin").mkdir(parents=True)
    (root / "sdk" / "default" / "openharmony" / "toolchains").mkdir(parents=True)
    (root / "sdk" / "default" / "openharmony" / "toolchains" / "hdc").write_text("")
    return root


def test_posix_uses_shell_wrapper_not_bat(tmp_path, monkeypatch):
    monkeypatch.setattr(ds.sys, "platform", "darwin")
    root = _fake_deveco(tmp_path / "Contents")
    hv = ds._resolve_hvigorw(tmp_path / "proj", str(root))
    assert hv is not None and hv.name == "hvigorw"
    cmd = ds._hvigorw_cmd(hv)
    assert cmd[0] != "cmd" and cmd[1:] == ["assembleHap", "--no-daemon"]


def test_windows_keeps_bat_via_cmd(tmp_path, monkeypatch):
    monkeypatch.setattr(ds.sys, "platform", "win32")
    root = _fake_deveco(tmp_path / "DevEco Studio")
    hv = ds._resolve_hvigorw(tmp_path / "proj", str(root))
    assert hv is not None and hv.name == "hvigorw.bat"
    assert ds._hvigorw_cmd(hv)[:2] == ["cmd", "/c"]


def test_project_local_wrapper_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(ds.sys, "platform", "darwin")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "hvigorw").write_text("#!/bin/sh\n")
    root = _fake_deveco(tmp_path / "Contents")
    assert ds._resolve_hvigorw(proj, str(root)) == proj / "hvigorw"


def test_root_falls_back_to_java_home(tmp_path, monkeypatch):
    root = _fake_deveco(tmp_path / "Contents")
    monkeypatch.setenv("JAVA_HOME", str(root / "jbr" / "Contents" / "Home"))
    assert ds._resolve_deveco_root(None) == root
    monkeypatch.delenv("DEVECO_SDK_HOME", raising=False)
    assert ds._resolve_deveco_sdk(None) == str(root / "sdk")


def test_hdc_found_without_config(tmp_path, monkeypatch):
    root = _fake_deveco(tmp_path / "Contents")
    monkeypatch.setenv("JAVA_HOME", str(root / "jbr" / "Contents" / "Home"))
    monkeypatch.setattr(ds.shutil, "which", lambda _name: None)
    monkeypatch.delenv("HDC", raising=False)
    found = ds._resolve_hdc_exe(None)
    assert found == str(root / "sdk" / "default" / "openharmony" / "toolchains" / "hdc")


@pytest.mark.parametrize("platform_name", ["darwin", "linux"])
def test_no_bat_on_posix_even_if_only_bat_exists(tmp_path, monkeypatch, platform_name):
    monkeypatch.setattr(ds.sys, "platform", platform_name)
    root = tmp_path / "Contents"
    (root / "tools" / "hvigor" / "bin").mkdir(parents=True)
    (root / "tools" / "hvigor" / "bin" / "hvigorw.bat").write_text("@echo off\n")
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.setattr(ds._deveco, "default_root", lambda: None)
    assert ds._resolve_hvigorw(tmp_path / "proj", str(root)) is None
