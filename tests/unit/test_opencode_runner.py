"""OpencodeRunner 的参数构造。"""
from __future__ import annotations

from pathlib import Path

from ecat.runner.opencode import OpencodeRunner, _rewrite_agent_frontmatter


def _runner(tmp_path: Path, add_dirs: list[Path] | None = None) -> OpencodeRunner:
    return OpencodeRunner(config_dir=tmp_path / "cfg", add_dirs=add_dirs)


def test_dir_is_cwd_only(tmp_path: Path) -> None:
    """--dir 只跟 cwd 走。"""
    cwd = tmp_path / "harmony"
    cwd.mkdir()
    assert _runner(tmp_path)._build_cwd_args(cwd) == ["--dir", str(cwd.resolve())]


def test_add_dirs_do_not_widen_dir(tmp_path: Path) -> None:
    """add_dirs 不再被折成公共祖先 —— opencode 的 --dir 不是访问边界。

    回归护栏：折公共祖先会让 --dir 退到整个 workspace。
    """
    cwd = tmp_path / "harmony"
    cwd.mkdir()
    android = tmp_path / "android"
    android.mkdir()
    args = _runner(tmp_path, add_dirs=[android])._build_cwd_args(cwd)
    assert args == ["--dir", str(cwd.resolve())]   # 不是 tmp_path（二者的公共祖先）


def test_no_cwd_no_dir_flag(tmp_path: Path) -> None:
    assert _runner(tmp_path)._build_cwd_args(None) == []


def test_frontmatter_drops_unknown_color(tmp_path: Path) -> None:
    """认不出的颜色要删行，透传会让 opencode 判整个配置非法。"""
    out = _rewrite_agent_frontmatter(
        "---\nname: x\ndescription: d\ncolor: chartreuse\n---\nbody\n",
    )
    assert "chartreuse" not in out
    assert "description: d" in out


def test_frontmatter_translates_yaml_list_tools() -> None:
    """`tools: ["Read","Glob"]` 也要翻译成 object。

    回归护栏：早先按"已是 list 就别动"放行，opencode 判整份配置非法、
    整个 role 起不来（compose-fact-analyzer.md 实测）。
    """
    out = _rewrite_agent_frontmatter(
        '---\nname: x\ndescription: d\ntools: ["Read","Glob","Bash"]\n---\nbody\n',
    )
    assert "tools:\n  read: true\n  glob: true\n  bash: true" in out
    assert "[" not in out.split("---")[1]


def test_frontmatter_keeps_native_object_tools() -> None:
    """opencode 原生的 object 形式原样保留。"""
    src = "---\nname: x\ndescription: d\ntools: {read: true}\n---\nbody\n"
    assert "tools: {read: true}" in _rewrite_agent_frontmatter(src)


def test_frontmatter_translates_multiline_list_tools() -> None:
    """多行列表形式 —— compose-fact-analyzer.md 的真实写法。

    `tools:` 独占一行、工具名在后续 `  - Read` 各行。续行必须一起消费掉，
    否则原样透传给 opencode 仍是 list，整个 role 起不来。
    """
    src = ('---\nname: x\ndescription: d\n'
           'tools:\n  - Read\n  - Glob\n  - Bash\n'
           'color: cyan\n---\nbody\n')
    out = _rewrite_agent_frontmatter(src)
    assert "tools:\n  read: true\n  glob: true\n  bash: true" in out
    assert "- Read" not in out.split("---")[1]
    assert 'color: "#00aaaa"' in out       # 后续字段不能被续行消费吃掉


def test_frontmatter_keeps_other_list_fields() -> None:
    """只翻译 tools，别的多行列表（如 skills:）原样保留。"""
    src = ('---\nname: x\ndescription: d\n'
           'skills:\n  - compose-fact-tree\n---\nbody\n')
    out = _rewrite_agent_frontmatter(src)
    assert "skills:\n  - compose-fact-tree" in out
