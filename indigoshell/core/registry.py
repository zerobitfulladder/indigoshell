from ..windows.base import WindowKind
from ..windows.bar import BarKind


def build_registry(config: dict) -> dict[str, WindowKind]:
    """Merge built-in kinds with any kinds declared in config["windows"]."""
    kinds: dict[str, WindowKind] = {"bar": BarKind()}
    for name, kind in (config.get("windows") or {}).items():
        if not kind.name:
            kind.name = name
        kinds[kind.name] = kind
    return kinds
