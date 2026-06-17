"""
Engine registry.

Each engine handles one block type ({attach:...}, {iscript:...}, etc.).
New engines can be added by subclassing BaseEngine, setting `name`, and
calling register().
"""

from __future__ import annotations


class EngineResult:
    __slots__ = ("text", "files", "error")

    def __init__(self, text: str = "", files=None, error: str = ""):
        self.text: str = text
        self.files: list = files or []
        self.error: str = error


class BaseEngine:
    name: str = ""

    async def execute(self, content: str, ctx, tag_ctx: dict) -> EngineResult:
        raise NotImplementedError


_registry: dict[str, BaseEngine] = {}


def register(engine: BaseEngine) -> None:
    _registry[engine.name] = engine


def get(name: str) -> BaseEngine | None:
    return _registry.get(name)


# Import and auto-register all built-in engines
from .attach import AttachEngine
from .iscript import IScriptEngine
from .mediascript import MediaScriptEngine
from .pyscript import PyScriptEngine

register(AttachEngine())
register(IScriptEngine())
register(MediaScriptEngine())
register(PyScriptEngine())

__all__ = [
    "EngineResult",
    "BaseEngine",
    "register",
    "get",
    "AttachEngine",
    "IScriptEngine",
    "MediaScriptEngine",
    "PyScriptEngine",
]
