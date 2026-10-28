"""One-time migration: flatten generator memory into a category tree.

Usage:
    python scripts/migrate_memory_tree.py <memory_dir> [--dry-run]

<memory_dir> is typically:
    <workspace>/.ecat/agents/generator/memory
"""

import argparse
from pathlib import Path

from ecat.memory.tree import classify_entry, migrate_flat_to_tree, _iter_flat_entries


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("memory_dir", type=Path, help="Path to generator memory dir")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print planned classification without moving files")
    args = parser.parse_args()

    memory_dir: Path = args.memory_dir
    if not (memory_dir / "MEMORY.md").exists():
        raise SystemExit(f"No MEMORY.md under {memory_dir} — is this a generator memory dir?")

    if args.dry_run:
        for path, name, description in _iter_flat_entries(memory_dir):
            print(f"{classify_entry(name, description):16} <- {path.name}")
        return

    migrate_flat_to_tree(memory_dir)
    print(f"Migrated memory tree under {memory_dir.resolve().as_posix()}")


if __name__ == "__main__":
    main()
