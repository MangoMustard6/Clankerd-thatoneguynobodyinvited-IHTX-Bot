"""
Engine registry.

Each engine handles one block type ({attach:...}, {iscript:...}, etc.).
New engines can be added by subclassing BaseEngine, setting `name`, and
calling register().
"""

from __future__ import annotations


class EngineResult:
    __slots__ = ("text", "embed", "files", "error")

    def __init__(self, text: str = "", embed=None, files=None, error: str = ""):
        self.text: str = text
        self.embed = embed
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
from .embedjson import EmbedJSONEngine
from .ihtx_engine import IHTXEngine
from .text_engine import TextEngine
from .eval_engine import EvalEngine
from .tagscript_engine import TagScriptEngine
from .bash_engine import BashEngine

register(AttachEngine())
register(IScriptEngine())
register(MediaScriptEngine())
register(PyScriptEngine())
register(EmbedJSONEngine())
register(IHTXEngine())
register(TextEngine())
register(EvalEngine())
register(TagScriptEngine())
register(BashEngine())

__all__ = [
    "EngineResult",
    "BaseEngine",
    "register",
    "get",
    "AttachEngine",
    "IScriptEngine",
    "MediaScriptEngine",
    "PyScriptEngine",
    "EmbedJSONEngine",
    "IHTXEngine",
    "TextEngine",
    "EvalEngine",
    "TagScriptEngine",
    "BashEngine",
]
