"""Unit tests for workspace seed functionality."""

from pathlib import Path
from unittest.mock import patch

from ecat.gradient.skill_catalog import _parse_frontmatter
from ecat.workspace import Workspace, _seed_dir


def test_seed_dir_found():
    seed = _seed_dir()
    assert seed is not None
    assert seed.is_dir()
    assert (seed / "agents" / "generator" / "CLAUDE.md").exists()
    assert (seed / "agents" / "discriminator" / "CLAUDE.md").exists()


def test_init_seeds_claude_md(tmp_path: Path):
    ws = Workspace(tmp_path)
    ws.init()

    gen_claude = tmp_path / ".ecat" / "agents" / "generator" / "CLAUDE.md"
    disc_claude = tmp_path / ".ecat" / "agents" / "discriminator" / "CLAUDE.md"
    assert gen_claude.exists()
    assert disc_claude.exists()
    assert "Generator" in gen_claude.read_text(encoding="utf-8")
    assert "Discriminator" in disc_claude.read_text(encoding="utf-8")


def test_init_seeds_skills(tmp_path: Path):
    ws = Workspace(tmp_path)
    ws.init()

    gen_skills = tmp_path / ".ecat" / "agents" / "generator" / "skills"
    assert gen_skills.is_dir()
    skill_dirs = [d.name for d in gen_skills.iterdir() if d.is_dir()]
    # 只断言"至少有一个可加载的 skill"，不绑数量和名字 —— skill 集会随迁移
    # 经验增删（当前只发布 harmonyos-build-fix 一个）。
    # 断言的是"这批 skill 真能被 opencode / Claude Code 加载"这条不变量：
    # 每个目录都有 SKILL.md，且 frontmatter 的 name + description 齐全
    # （缺 name 的 skill opencode 会静默跳过 —— 实测判别器 4 个里 3 个中招）。
    assert skill_dirs, "generator/skills 下没有任何 skill 目录"
    for name in skill_dirs:
        md = gen_skills / name / "SKILL.md"
        assert md.is_file(), f"{name} 缺 SKILL.md"
        fm = _parse_frontmatter(md)
        assert fm.get("name"), f"{name} 的 frontmatter 缺 name"
        assert fm.get("description"), f"{name} 的 frontmatter 缺 description"


def test_init_seeds_subagents(tmp_path: Path):
    ws = Workspace(tmp_path)
    ws.init()

    # subagent 模板挂在 discriminator 下（generator 侧只在需要时由判别器派生）。
    disc_agents = tmp_path / ".ecat" / "agents" / "discriminator" / "agents"
    assert disc_agents.is_dir()
    agent_files = [f.name for f in disc_agents.iterdir() if f.is_file()]
    assert agent_files, "discriminator/agents 下没有任何 subagent 模板"
    for name in agent_files:
        fm = _parse_frontmatter(disc_agents / name)
        assert fm.get("name"), f"{name} 的 frontmatter 缺 name"
        assert fm.get("description"), f"{name} 的 frontmatter 缺 description"


def test_init_seeds_prompts(tmp_path: Path):
    ws = Workspace(tmp_path)
    ws.init()

    prompts = tmp_path / ".ecat" / "agents" / "prompts"
    assert prompts.is_dir()
    prompt_files = [f.name for f in prompts.iterdir() if f.is_file()]
    assert len(prompt_files) >= 1


def test_init_no_overwrite_existing(tmp_path: Path):
    ws = Workspace(tmp_path)
    ws.init()

    gen_claude = tmp_path / ".ecat" / "agents" / "generator" / "CLAUDE.md"
    gen_claude.write_text("# Custom config\n", encoding="utf-8")

    ws.init()

    assert gen_claude.read_text(encoding="utf-8") == "# Custom config\n"


def test_init_force_overwrites(tmp_path: Path):
    ws = Workspace(tmp_path)
    ws.init()

    gen_claude = tmp_path / ".ecat" / "agents" / "generator" / "CLAUDE.md"
    gen_claude.write_text("# Custom config\n", encoding="utf-8")

    ws.init(force=True)

    content = gen_claude.read_text(encoding="utf-8")
    assert "Generator" in content
    assert "Custom config" not in content


def test_init_no_seed_dir_graceful(tmp_path: Path):
    ws = Workspace(tmp_path)
    with patch("ecat.workspace._seed_dir", return_value=None):
        ws.init()

    gen_skills = tmp_path / ".ecat" / "agents" / "generator" / "skills"
    assert gen_skills.is_dir()
    assert list(gen_skills.iterdir()) == []


def test_seed_disc_skills(tmp_path: Path):
    ws = Workspace(tmp_path)
    ws.init()

    disc_skills = tmp_path / ".ecat" / "agents" / "discriminator" / "skills"
    assert disc_skills.is_dir()
    skill_dirs = [d.name for d in disc_skills.iterdir() if d.is_dir()]
    assert "score-smells" in skill_dirs
