"""
TagScript parser — resolves variables and dispatches engine blocks.

Syntax:
  Variables  : {varname}  /  {varname.N}
  Functions  : {random:a|b|c}  {random:1:100}  {choose:a|b|c}
               {upper:text}  {lower:text}  {title:text}  {reverse:text}
               {len:text}  {repeat:N|text}  {slice:s|e|text}
               {math:1+2}  {if:a|=|b|then:yes}
  Engines    : {attach:URL}  {iscript:...}  {mediascript:...}
               {embedjson:{...}}  {ihtx:reps dur noTrim fmt effects}  {py:...}
               Engine blocks are stripped from text and returned separately.
"""

from __future__ import annotations

import random
import re
from typing import Any

# ── Regex patterns ────────────────────────────────────────────────────────────

_VAR_RE = re.compile(r"\{([a-z_][a-z0-9_]*(?:\.[a-z0-9_]+)?)\}", re.IGNORECASE)

# Matches {name:content} — content may contain ONE level of nested {}.
_BLOCK_RE = re.compile(
    r"\{([a-z_][a-z0-9_]*):((?:[^{}]|\{[^{}]*\})*)\}",
    re.DOTALL | re.IGNORECASE,
)

# Engines whose content may contain deeply nested braces (e.g. JSON objects).
# These are extracted by a depth-counting pre-pass before the regex runs.
_DEEP_ENGINES = ("embedjson",)

# All engine names — blocks with these names are dispatched to engines.
KNOWN_ENGINES = frozenset({"attach", "iscript", "mediascript", "py", "embedjson", "ihtx"})


# ── Safe math ─────────────────────────────────────────────────────────────────

class _MathParser:
    """Recursive-descent math parser — no eval/exec used.

    Supports: + - * / % ^ (power), unary minus, parentheses, integer and
    floating-point literals.
    """

    _TOK = re.compile(r"\d+\.?\d*|[+\-*/^%()\s]")

    def __init__(self, expr: str):
        raw = "".join(self._TOK.findall(expr))
        self._tokens = [t for t in re.split(r"(\d+\.?\d*|[+\-*/^%()])", raw) if t.strip()]
        self._pos = 0

    def _peek(self) -> str | None:
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def _eat(self) -> str:
        t = self._tokens[self._pos]
        self._pos += 1
        return t

    def parse(self) -> float:
        v = self._expr()
        if self._pos < len(self._tokens):
            raise ValueError(f"unexpected token: {self._peek()!r}")
        return v

    def _expr(self) -> float:
        return self._additive()

    def _additive(self) -> float:
        left = self._mult()
        while self._peek() in ("+", "-"):
            op = self._eat()
            right = self._mult()
            left = left + right if op == "+" else left - right
        return left

    def _mult(self) -> float:
        left = self._power()
        while self._peek() in ("*", "/", "%"):
            op = self._eat()
            right = self._power()
            if op == "*":
                left *= right
            elif op == "/":
                if right == 0:
                    raise ValueError("division by zero")
                left /= right
            else:
                left %= right
        return left

    def _power(self) -> float:
        left = self._unary()
        if self._peek() == "^":
            self._eat()
            right = self._unary()
            left = left ** right
        return left

    def _unary(self) -> float:
        if self._peek() == "-":
            self._eat()
            return -self._primary()
        if self._peek() == "+":
            self._eat()
        return self._primary()

    def _primary(self) -> float:
        t = self._peek()
        if t == "(":
            self._eat()
            v = self._expr()
            if self._peek() != ")":
                raise ValueError("missing closing paren")
            self._eat()
            return v
        if t is not None and re.fullmatch(r"\d+\.?\d*", t):
            self._eat()
            return float(t)
        raise ValueError(f"unexpected: {t!r}")


def _safe_math(expr: str) -> str:
    try:
        result = _MathParser(expr.strip()).parse()
        if result == int(result) and abs(result) < 1e15:
            return str(int(result))
        return f"{result:.10g}"
    except Exception as exc:
        return f"[math error: {exc}]"


# ── Deep-block pre-pass ───────────────────────────────────────────────────────

def _extract_deep_blocks(text: str) -> tuple[str, list[dict]]:
    """Extract {engine:...} blocks whose content may contain nested braces.

    Scans character-by-character counting brace depth so JSON objects inside
    {embedjson:...} are captured correctly.
    Returns (remaining_text, list_of_engine_block_dicts).
    """
    engine_blocks: list[dict] = []

    for engine_name in _DEEP_ENGINES:
        prefix = "{" + engine_name + ":"
        prefix_lo = prefix.lower()
        result_chars: list[str] = []
        i = 0
        text_lo = text.lower()

        while i < len(text):
            if text_lo[i:i + len(prefix_lo)] == prefix_lo:
                # Found the opening — now count braces to find the real close
                start_content = i + len(prefix)
                depth = 1
                j = start_content
                while j < len(text) and depth > 0:
                    if text[j] == "{":
                        depth += 1
                    elif text[j] == "}":
                        depth -= 1
                    j += 1
                if depth == 0:
                    content = text[start_content : j - 1].strip()
                    engine_blocks.append({"engine": engine_name, "content": content})
                    i = j
                    text_lo = text.lower()  # keep in sync (no mutation actually needed)
                    continue
            result_chars.append(text[i])
            i += 1

        text = "".join(result_chars)

    return text, engine_blocks


# ── Variable resolution ───────────────────────────────────────────────────────

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


# ── Block processing ──────────────────────────────────────────────────────────

def _eval_if(content: str) -> str:
    """Evaluate {if:a|op|b|then:result} conditional."""
    parts = content.split("|")
    if len(parts) < 4:
        return ""
    a, op, b = parts[0], parts[1], parts[2]
    then_rest = "|".join(parts[3:])
    if not then_rest.startswith("then:"):
        return ""
    result_val = then_rest[5:]

    try:
        a_num, b_num = float(a), float(b)
        numeric = True
    except ValueError:
        numeric = False

    matched = False
    if op in ("=", "=="):
        matched = a == b
    elif op == "!=":
        matched = a != b
    elif op == ">" and numeric:
        matched = a_num > b_num
    elif op == "<" and numeric:
        matched = a_num < b_num
    elif op == ">=" and numeric:
        matched = a_num >= b_num
    elif op == "<=" and numeric:
        matched = a_num <= b_num

    return result_val if matched else ""


def _handle_block(name: str, content: str, engine_blocks: list) -> str:
    name = name.lower()

    # ── Engines ──
    if name in KNOWN_ENGINES:
        engine_blocks.append({"engine": name, "content": content.strip()})
        return ""

    # ── Conditionals ──
    if name == "if":
        return _eval_if(content)

    # ── Math ──
    if name == "math":
        return _safe_math(content)

    # ── Random ──
    if name in ("random", "choose"):
        # {random:min:max} — colon-separated numeric range
        if ":" in content and "|" not in content:
            halves = content.split(":", 1)
            try:
                lo, hi = int(halves[0].strip()), int(halves[1].strip())
                return str(random.randint(min(lo, hi), max(lo, hi)))
            except ValueError:
                pass
        # {random:a|b|c} / {choose:a|b|c} — pipe-separated choices
        choices = [c.strip() for c in content.split("|") if c.strip()]
        return random.choice(choices) if choices else ""

    # ── Text functions ──
    if name == "upper":
        return content.upper()

    if name == "lower":
        return content.lower()

    if name == "title":
        return content.title()

    if name == "reverse":
        return content[::-1]

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

    # Unknown block — leave intact
    return "{" + name + ":" + content + "}"


def resolve_blocks(text: str, ctx: dict) -> tuple[str, list[dict]]:
    """
    Process function blocks and extract engine blocks.
    Returns (processed_text, list_of_engine_block_dicts).
    """
    engine_blocks: list[dict] = []

    def replacer(m: re.Match) -> str:
        return _handle_block(m.group(1), m.group(2), engine_blocks)

    processed = _BLOCK_RE.sub(replacer, text)
    return processed.strip(), engine_blocks


# ── Top-level ─────────────────────────────────────────────────────────────────

def parse(content: str, ctx: dict) -> tuple[str, list[dict]]:
    """Full TagScript parse: resolve variables, deep-engine pre-pass, then blocks."""
    # 1. Variable substitution
    text = resolve_variables(content, ctx)
    # 2. Extract engines with potentially nested braces (e.g. embedjson)
    text, deep_blocks = _extract_deep_blocks(text)
    # 3. Extract remaining function/engine blocks via regex
    text, shallow_blocks = resolve_blocks(text, ctx)
    return text, deep_blocks + shallow_blocks


def build_context(discord_ctx, args: str) -> dict:
    """Build the variable context dict from a discord.py Context object."""
    user = discord_ctx.author
    guild = discord_ctx.guild
    channel = discord_ctx.channel

    args = args.strip()

    nick = None
    if hasattr(user, "nick"):
        nick = user.nick
    nickname = nick or user.display_name

    return {
        # User
        "user": user.display_name,
        "username": user.name,
        "userid": str(user.id),
        "id": str(user.id),
        "mention": user.mention,
        "avatar": str(user.display_avatar.url),
        "nickname": nickname,
        "usertag": str(user),
        # Server
        "server": guild.name if guild else "DM",
        "serverid": str(guild.id) if guild else "0",
        # Channel
        "channel": getattr(channel, "name", "DM"),
        "channelid": str(channel.id),
        # Args
        "args": args,
        "argslen": str(len(args.split()) if args else 0),
    }
