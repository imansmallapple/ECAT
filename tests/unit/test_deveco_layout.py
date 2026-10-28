"""DevEco install layout detection across Windows / macOS / Linux."""

from pathlib import Path

from ecat import deveco


def _make_root(tmp_path: Path, *, mac: bool) -> Path:
    root = tmp_path / ("DevEco-Studio.app/Contents" if mac else "DevEco Studio")
    (root / "tools" / "hvigor" / "bin").mkdir(parents=True)
    (root / "sdk").mkdir()
    if mac:
        (root / "jbr" / "Contents" / "Home" / "bin").mkdir(parents=True)
        (root / "tools" / "node" / "bin").mkdir(parents=True)
    else:
        (root / "jbr" / "bin").mkdir(parents=True)
        (root / "tools" / "node").mkdir(parents=True)
    return root


def test_windows_layout(tmp_path: Path):
    root = _make_root(tmp_path, mac=False)
    assert deveco.jdk_home(root) == root / "jbr"
    assert deveco.node_bin_dir(root) == root / "tools" / "node"
    assert deveco.root_from_java_home(root / "jbr") == root


def test_macos_layout(tmp_path: Path):
    root = _make_root(tmp_path, mac=True)
    assert deveco.jdk_home(root) == root / "jbr" / "Contents" / "Home"
    assert deveco.node_bin_dir(root) == root / "tools" / "node" / "bin"
    assert deveco.root_from_java_home(root / "jbr" / "Contents" / "Home") == root
    assert deveco.root_from_java_home(root / "jbr") == root


def test_unrelated_java_home(tmp_path: Path):
    assert deveco.root_from_java_home(tmp_path / "some-jdk") is None
    assert deveco.root_from_java_home(None) is None


def test_compile_env_uses_macos_jdk(tmp_path: Path):
    from ecat.entropy.plugins.a2h_verify.check_compile import _build_env

    root = _make_root(tmp_path, mac=True)
    hvigorw = root / "tools" / "hvigor" / "bin" / "hvigorw"
    hvigorw.touch()
    env = _build_env(hvigorw)
    assert env["JAVA_HOME"] == str(root / "jbr" / "Contents" / "Home")
    assert env["DEVECO_SDK_HOME"] == str(root / "sdk")
    assert str(root / "tools" / "node" / "bin") in env["PATH"]
