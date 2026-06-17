"""
TagScript parser — resolves variables and dispatches engine blocks.

Syntax:
  Variables  : {varname}  /  {varname.N}
  Functions  : {random:a|b|c}  {upper:text}  {lower:text}  {repeat:N|text}  {len:text}
  Engines    : {attach:URL}  {iscript:...}  {mediascript:...}  {py:...}
               Engine blocks are stripped from text and returned separately.
"""

import re
import random
from typing import Any

_VAR_RE = re.compile(r"\{([a-z_][a-z0-9_]*(?:\.[a-z0-9_]+)?)\}", re.IGNORECASE)

# Matches {name:content} where content may span multiple lines but not contain
# unbalanced braces (safe for most real-world tag scripts).
_BLOCK_RE = re.compile(
    r"\{([a-z_][a-z0-9_]*):((?:[^{}]|\{[^{}]*\})*)\}",
    re.DOTALL | re.IGNORECASE,
)

KNOWN_ENGINES = frozenset({"attach", "iscript", "mediascript", "py"})


# ---------------------------------------------------------------------------
# Variable resolution
# ---------------------------------------------------------------------------

def _resolve_var(key: str, ctx: dict) -> str:
    key = key.lower()

    # args.N — 1-indexed word from args
    if key.startswith("args."):
        try:
            idx = int(key.split(".", 1)[1]) - 1
            words = ctx.get("args", "").split()
            return words[idx] if 0 <= idx < len(words) else ""
        except (ValueError, IndexError):
            return ""

    val = ctx.get(key)
    return str(val) if val is not None else ""


def resolve_variables(text: str, ctx: dict) -> str:
    """Replace {var} / {var.N} placeholders with context values."""

    def replacer(m: re.Match) -> str:
        result = _resolve_var(m.group(1), ctx)
        return result if result else m.group(0)

    return _VAR_RE.sub(replacer, text)


# ---------------------------------------------------------------------------
# Block processing
# ---------------------------------------------------------------------------

def _handle_block(name: str, content: str, engine_blocks: list) -> str:
    name = name.lower()

    if name in KNOWN_ENGINES:
        engine_blocks.append({"engine": name, "content": content.strip()})
        return ""

    if name == "random" or name == "choose":
        choices = [c.strip() for c in content.split("|") if c.strip()]
        return random.choice(choices) if choices else ""

    if name == "upper":
        return content.upper()

    if name == "lower":
        return content.lower()

    if name == "len":
        return str(len(content.strip()))

    if name == "repeat":
        parts = content.split("|", 1)
        try:
            n = max(0, min(int(parts[0].strip()), 20))
            val = parts[1].strip() if len(parts) > 1 else ""
            return val * n
        except ValueError:
            return ""

    if name == "slice":
        # {slice:start|end|text}
        parts = content.split("|", 2)
        if len(parts) == 3:
            try:
                s, e = int(parts[0]), int(parts[1])
                return parts[2][s:e]
            except (ValueError, IndexError):
                pass
        return ""

    # Unknown block: leave intact
    return "{" + name + ":" + content + "}"


def resolve_blocks(text: str, ctx: dict) -> tuple:
    """
    Process function blocks and extract engine blocks.
    Returns (processed_text, list_of_engine_block_dicts).
    """
    engine_blocks: list = []

    def replacer(m: re.Match) -> str:
        return _handle_block(m.group(1), m.group(2), engine_blocks)

    processed = _BLOCK_RE.sub(replacer, text)
    return processed.strip(), engine_blocks


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------

def parse(content: str, ctx: dict) -> tuple:
    """Full TagScript parse: resolve variables then function/engine blocks."""
    text = resolve_variables(content, ctx)
    text, engine_blocks = resolve_blocks(text, ctx)
    return text, engine_blocks


def build_context(discord_ctx, args: str) -> dict:
    """Build the variable context dict from a discord.py Context object."""
    user = discord_ctx.author
    guild = discord_ctx.guild
    channel = discord_ctx.channel

    args = args.strip()
    return {
        "args": args,
        "user": user.display_name,
        "mention": user.mention,
        "id": str(user.id),
        "avatar": str(user.display_avatar.url),
        "server": guild.name if guild else "DM",
        "channel": getattr(channel, "name", "DM"),
        "argslen": str(len(args.split()) if args else 0),
        "usertag": str(user),
    }
