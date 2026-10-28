"""Plugin registry — maps plugin set names to lists of EntropyPlugin instances."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ecat.entropy.base import EntropyPlugin

_REGISTRY: dict[str, list[EntropyPlugin]] = {}


class PluginRegistry:
    """Global registry for entropy plugin sets."""

    @staticmethod
    def register(set_name: str, plugins: list[EntropyPlugin]) -> None:
        _REGISTRY[set_name] = plugins

    @staticmethod
    def get(set_name: str) -> list[EntropyPlugin]:
        if set_name not in _REGISTRY:
            raise KeyError(
                f"Unknown plugin set {set_name!r}. "
                f"Available: {list(_REGISTRY.keys())}"
            )
        return _REGISTRY[set_name]

    @staticmethod
    def list_sets() -> list[str]:
        return list(_REGISTRY.keys())
