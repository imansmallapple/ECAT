"""DevEco Studio layout helpers shared by the compile plugin and the CLI.

The install layout differs per platform:

- Windows: ``<root>/jbr/bin/java.exe``, ``<root>/tools/node/node.exe``
- macOS:   ``<root>`` is ``DevEco-Studio.app/Contents``; the JDK lives in
           ``<root>/jbr/Contents/Home`` and node in ``<root>/tools/node/bin``
- Linux:   ``<root>/jbr/bin/java``, ``<root>/tools/node/bin/node``
"""

from __future__ import annotations

from pathlib import Path

# Default install locations probed when nothing is configured.
_DEFAULT_ROOTS = (
    Path("/Applications/DevEco-Studio.app/Contents"),
    Path.home() / "Applications" / "DevEco-Studio.app" / "Contents",
)


def is_deveco_root(path: Path) -> bool:
    return (path / "tools" / "hvigor").is_dir()


def jdk_home(root: Path) -> Path | None:
    """JDK home bundled with DevEco (the value JAVA_HOME should take)."""
    jbr = root / "jbr"
    mac_home = jbr / "Contents" / "Home"
    if mac_home.is_dir():
        return mac_home
    return jbr if jbr.is_dir() else None


def node_bin_dir(root: Path) -> Path | None:
    """Directory holding the bundled node executable."""
    node = root / "tools" / "node"
    if (node / "bin").is_dir():
        return node / "bin"
    return node if node.is_dir() else None


def root_from_java_home(java_home: str | Path | None) -> Path | None:
    """Walk up from JAVA_HOME to the DevEco root.

    Covers ``<root>/jbr`` (Windows/Linux) and ``<root>/jbr/Contents/Home`` (macOS).
    """
    if not java_home:
        return None
    for parent in list(Path(java_home).parents)[:3]:
        if is_deveco_root(parent):
            return parent
    return None


def default_root() -> Path | None:
    for root in _DEFAULT_ROOTS:
        if is_deveco_root(root):
            return root
    return None
