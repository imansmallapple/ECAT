"""Unit tests for the generator memory tree (classify / migrate / render)."""

from pathlib import Path

from ecat.gradient.builder import _build_memory_section
from ecat.memory.tree import classify_entry, migrate_flat_to_tree, render_injection


def test_classify_navigation():
    assert classify_entry("router-getparams-navpathstack",
                           "router.getParams() returns stale data when reached via NavPathStack") == "navigation"


def test_classify_native_napi():
    assert classify_entry("napi-manual-zero-stub",
                           "C++ NAPI functions that manually return 0/false/null bypass DEFINE_STUB grep") == "native-napi"


def test_classify_state_storage():
    assert classify_entry("appstorage-restore-on-appear",
                           "@State persisted via AppStorage.setOrCreate() must be restored via AppStorage.get()") == "state-storage"


def test_classify_arkts_idioms():
    assert classify_entry("arkts-no-var", "ArkTS disallows var — use const/let instead") == "arkts-idioms"


def test_classify_unknown_falls_back_to_misc():
    assert classify_entry("totally-unrelated", "something with no known keywords zzz") == "misc"


def _write_flat_entry(memory_dir: Path, slug: str, description: str) -> None:
    (memory_dir / f"feedback_{slug}.md").write_text(
        f"---\nname: {slug}\ndescription: {description}\nmetadata:\n  type: feedback\n---\nbody\n",
        encoding="utf-8",
    )


def test_migrate_moves_files_into_category_dirs(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    _write_flat_entry(mem, "arkts-no-var", "ArkTS disallows var")
    _write_flat_entry(mem, "router-getparams-navpathstack", "router.getParams stale under NavPathStack")
    (mem / "MEMORY.md").write_text("# Generator Memory Index\n- old line\n", encoding="utf-8")

    migrate_flat_to_tree(mem)

    assert (mem / "arkts-idioms" / "feedback_arkts-no-var.md").exists()
    assert (mem / "navigation" / "feedback_router-getparams-navpathstack.md").exists()
    # flat copies removed from root
    assert not (mem / "feedback_arkts-no-var.md").exists()


def test_migrate_writes_sub_index_and_top_index(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    _write_flat_entry(mem, "arkts-no-var", "ArkTS disallows var")

    migrate_flat_to_tree(mem)

    sub_index = (mem / "arkts-idioms" / "_index.md").read_text(encoding="utf-8")
    assert "feedback_arkts-no-var.md" in sub_index
    assert "ArkTS disallows var" in sub_index

    top = (mem / "MEMORY.md").read_text(encoding="utf-8")
    assert "arkts-idioms" in top
    assert "arkts-idioms/_index.md" in top
    assert "(1)" in top  # count rendered
    assert "ArkTS language rules" in top  # category hint rendered


def test_migrate_is_idempotent(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    _write_flat_entry(mem, "arkts-no-var", "ArkTS disallows var")

    migrate_flat_to_tree(mem)
    first = (mem / "MEMORY.md").read_text(encoding="utf-8")
    migrate_flat_to_tree(mem)
    second = (mem / "MEMORY.md").read_text(encoding="utf-8")

    assert first == second
    assert (mem / "arkts-idioms" / "feedback_arkts-no-var.md").exists()


def test_migrate_includes_hyphen_named_entry(tmp_path):
    # Legacy hyphen-prefixed filename (most entries use feedback_, this one uses feedback-)
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "feedback-nav-hub-completeness.md").write_text(
        "---\nname: nav-hub-completeness\ndescription: nav hub must cover all targets\n"
        "metadata:\n  type: feedback\n---\nbody\n",
        encoding="utf-8",
    )

    migrate_flat_to_tree(mem)

    assert (mem / "navigation" / "feedback-nav-hub-completeness.md").exists()
    assert not (mem / "feedback-nav-hub-completeness.md").exists()


def test_render_injection_none_returns_empty():
    assert render_injection(None) == ""


def test_render_injection_missing_index_returns_empty(tmp_path):
    assert render_injection(tmp_path / "nope") == ""


def test_render_injection_lists_categories_and_drilldown_instruction(tmp_path):
    mem = tmp_path / "memory"
    (mem / "navigation").mkdir(parents=True)
    (mem / "navigation" / "_index.md").write_text("# navigation\n", encoding="utf-8")
    (mem / "MEMORY.md").write_text(
        "# Generator Memory Index\n\n- [navigation](navigation/_index.md) — 5 entries (5 direct)\n",
        encoding="utf-8",
    )

    out = render_injection(mem)

    assert "navigation/_index.md" in out          # category line carried through
    assert "drill" in out.lower()                  # recursive drill-down instruction
    assert "prune" in out.lower()                  # prune-irrelevant-branches instruction
    assert mem.resolve().as_posix() in out         # memory root path
    assert "Learned fix patterns" in out


def test_memory_section_disabled_returns_empty():
    assert _build_memory_section(False, Path("/x"), None) == ""
    assert _build_memory_section(True, None, None) == ""


def test_memory_section_encodes_tree_write_protocol():
    out = _build_memory_section(True, Path("/ws/memory"), None)
    assert "MEMORY.md" in out                 # step 1: read top index
    assert "_index.md" in out                 # step 2/5: read & update sub-index
    assert ">8" in out or "> 8" in out        # split threshold
    assert "depth" in out.lower() and "3" in out  # depth cap
    assert "kebab" in out.lower()
    assert "≤2" in out                         # per-iteration cap preserved


def test_memory_section_keeps_h_delta_hint():
    out = _build_memory_section(True, Path("/ws/memory"), -0.02)
    assert "↓" in out
