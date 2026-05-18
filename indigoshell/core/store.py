from collections import defaultdict
from typing import Any, Callable


class Store:
    """Reactive key/value store. Sources call `set`, widgets `subscribe`."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._subs: dict[str, list[Callable[[Any], None]]] = defaultdict(list)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        if self._data.get(key, _MISSING) == value:
            return
        self._data[key] = value
        for cb in list(self._subs[key]):
            cb(value)

    def subscribe(self, key: str, cb: Callable[[Any], None]) -> Callable[[], None]:
        self._subs[key].append(cb)
        return lambda: self._subs[key].remove(cb) if cb in self._subs[key] else None


_MISSING = object()
