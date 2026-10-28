"""Centralized prompt templates for ECAT."""

from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).parent


def load_prompt(name: str) -> str:
    """Load a prompt template by name (without extension)."""
    return (_DIR / f"{name}.txt").read_text(encoding="utf-8")
