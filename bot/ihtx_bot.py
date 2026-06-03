"""
IHTX Bot — I Hate The X FFmpeg Discord Bot

All effects go through one unified command:
  !ihtx effect1=val,effect2=val,...

Pipe syntax (comma-separated key=value):
  chaos, glitch, shake, rainbow, static, melt, corrupt
        =true  or  =N  (N passes of that effect)
  huehsv=0.5            hue amount 0-1
  pinch=1;0.5;0.5;0.5   strength;radius;cx;cy  (all optional, defaults shown)
  multipitch=0;7;12     semicolon-separated semitones (each shifted, then amixed)
  reverse=true          reverse video + audio (applied once at end)
  rep=N                 number of render cycles (default 1)
  duration=N            seconds per segment (default 0.5)
  concat=true           TRUE IHTX MODE — render→render→concat
                        each rep re-encodes the previous segment (artifacts compound),
                        then all segments are joined: total = rep × duration seconds
"""

import discord
from discord.ext import commands
import asyncio
import json
import os
import re
import tempfile
import subprocess
import aiohttp
import sys
import time
import urllib.parse
import hashlib
import datetime
from pathlib import Path

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    print("ERROR: DISCORD_TOKEN environment variable not set.", file=sys.stderr)
    sys.exit(1)

OWNER_ID = 1355759019330895973

# ─── Owner IDs (can have multiple owners via g!owner command) ───────────────

OWNER_IDS_FILE = Path("bot/owner_ids.json")
owner_ids: set[int] = {OWNER_ID}

def _load_owner_ids():
    global owner_ids
    if OWNER_IDS_FILE.exists():
        try:
            with open(OWNER_IDS_FILE) as f:
                owner_ids = set(json.load(f))
        except Exception:
            owner_ids = {OWNER_ID}
    else:
        owner_ids = {OWNER_ID}

def _save_owner_ids():
    OWNER_IDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OWNER_IDS_FILE, "w") as f:
        json.dump(list(owner_ids), f)

def _is_owner(ctx: commands.Context) -> bool:
    return ctx.author.id in owner_ids

def _is_owner_by_id(user_id: int) -> bool:
    return user_id in owner_ids

_load_owner_ids()

# ─── Heavy command rate limiting ───────────────────────────────────────────

# These commands are classified as "heavy" (CPU-intensive / FFmpeg)
HEAVY_COMMANDS = {"ihtx", "effect", "destroy", "preview1280", "p1280", "ihtxsync", "download", "dl"}

# Default limits: owner = 5340, everyone else = 10 per day
HEAVY_LIMIT_DEFAULT = 10
HEAVY_LIMIT_OWNER   = 5340

LIMITS_FILE = Path("bot/limits.json")
heavy_limits: dict[int, int] = {}  # per-user overrides
heavy_usage: dict[int, list[float]] = {}  # user_id -> list of epoch timestamps

def _load_limits():
    global heavy_limits
    if LIMITS_FILE.exists():
        try:
            with open(LIMITS_FILE) as f:
                heavy_limits = {int(k): v for k, v in json.load(f).items()}
        except Exception:
            heavy_limits = {}

def _save_limits():
    LIMITS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LIMITS_FILE, "w") as f:
        json.dump(heavy_limits, f)

def _check_heavy_limit(user_id: int) -> tuple[bool, str]:
    """Return (ok, reason) for a heavy command usage."""
    if _is_owner_by_id(user_id):
        return True, ""
    limit = heavy_limits.get(user_id, HEAVY_LIMIT_DEFAULT)
    now = time.time()
    day_ago = now - 86400
    # Clean old entries
    usage = [t for t in heavy_usage.get(user_id, []) if t > day_ago]
    heavy_usage[user_id] = usage
    if len(usage) >= limit:
        return False, f"Heavy command limit reached ({limit}/{limit} per 24h). Contact an owner."
    usage.append(now)
    return True, ""

_load_limits()

# ─── Blocklist (users) ───────────────────────────────────────────────────────

BLOCKLIST_FILE = Path("bot/blocklist.json")
blocklist: set[int] = set()

def _load_blocklist():
    global blocklist
    if BLOCKLIST_FILE.exists():
        try:
            with open(BLOCKLIST_FILE) as f:
                blocklist = set(json.load(f))
        except Exception:
            blocklist = set()

def _save_blocklist():
    BLOCKLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(BLOCKLIST_FILE, "w") as f:
        json.dump(list(blocklist), f)

_load_blocklist()

# ─── Channel blocklist ────────────────────────────────────────────────────────

CHANNEL_BLOCK_FILE = Path("bot/channel_blocks.json")
channel_blocks: set[int] = set()

def _load_channel_blocks():
    global channel_blocks
    if CHANNEL_BLOCK_FILE.exists():
        try:
            with open(CHANNEL_BLOCK_FILE) as f:
                channel_blocks = set(json.load(f))
        except Exception:
            channel_blocks = set()

def _save_channel_blocks():
    CHANNEL_BLOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CHANNEL_BLOCK_FILE, "w") as f:
        json.dump(list(channel_blocks), f)

_load_channel_blocks()

# ─── Tags (custom presets) ─────────────────────────────────────────────────────

TAGS_FILE = Path("bot/tags.json")
tags: dict[str, dict] = {}

def _load_tags():
    global tags
    if TAGS_FILE.exists():
        try:
            with open(TAGS_FILE) as f:
                tags = json.load(f)
        except Exception:
            tags = {}

def _save_tags():
    TAGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TAGS_FILE, "w") as f:
        json.dump(tags, f, indent=2)

_load_tags()

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="g!", intents=intents)


# ─── Global command checks ────────────────────────────────────────────────────

@bot.check
async def _global_checks(ctx: commands.Context) -> bool:
    # Channel blocked
    if ctx.channel.id in channel_blocks:
        return False
    # User blocked
    if ctx.author.id in blocklist:
        return False
    # Heavy command rate limiting
    if ctx.command and ctx.command.name in HEAVY_COMMANDS:
        ok, reason = _check_heavy_limit(ctx.author.id)
        if not ok:
            await ctx.reply(f"❌ {reason}")
            return False
    return True

# ─── Runtime stats ────────────────────────────────────────────────────────────

_bot_start_time: float = time.time()
_renders_completed: int = 0
_renders_in_progress: int = 0

SUPPORTED_EXTENSIONS  = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".gif", ".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXTENSIONS      = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".gif"}
AUDIO_VIDEO_EXTS      = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
MAX_FILE_SIZE         = 25 * 1024 * 1024
MAX_REPETITIONS       = 100
MAX_DURATION          = 600

# ─── Effect filter definitions ────────────────────────────────────────────────

_BASE_NOISE = "noise=alls=40:allf=t+u"
_SHAKE      = "crop=iw-20:ih-20:10+5*sin(t*30):10+5*cos(t*17),scale=iw+20:ih+20"
_CHROMAB = (
    "[IN]split=3[r][g][b];"
    "[r]lutrgb=r=val:g=0:b=0,pad=iw+6:ih:3:0[ro];"
    "[g]lutrgb=r=0:g=val:b=0[go];"
    "[b]lutrgb=r=0:g=0:b=val,pad=iw+6:ih:0:0[bo];"
    "[ro][go]blend=all_mode=addition[rg];"
    "[rg][bo]blend=all_mode=addition[OUT]"
)

# vf = simple -vf chain; complex = -filter_complex graph using [0:v] as input
PRESET_FILTERS: dict[str, dict] = {
    "chaos": {
        "vf": f"{_SHAKE},{_BASE_NOISE},hue=h=t*180:s=2,eq=contrast=1.5:brightness=0.05:saturation=3",
        "complex": None,
    },
    "glitch": {
        "vf": f"rgbashift=rh=8:rv=-8:gh=-4:gv=4:bh=6:bv=-6,{_BASE_NOISE},eq=contrast=1.8:saturation=0",
        "complex": None,
    },
    "shake": {
        "vf": f"{_SHAKE},{_BASE_NOISE},eq=contrast=1.3:saturation=1.5",
        "complex": None,
    },
    "rainbow": {
        "vf": None,
        "complex": (
            "[0:v]split=3[r][g][b];"
            "[r]lutrgb=r=val:g=0:b=0,pad=iw+6:ih:3:0[ro];"
            "[g]lutrgb=r=0:g=val:b=0[go];"
            "[b]lutrgb=r=0:g=0:b=val,pad=iw+6:ih:0:0[bo];"
            "[ro][go]blend=all_mode=addition[rg];"
            "[rg][bo]blend=all_mode=addition"
        ),
    },
    "static": {
        "vf": f"{_BASE_NOISE},curves=vintage,eq=contrast=1.2",
        "complex": None,
    },
    "melt": {
        "vf": (
            "perspective=x0=0:y0=0:x1=iw:y1=20*sin(t*3)"
            ":x2=0:y2=ih:x3=iw:y3=ih-20*sin(t*3),"
            + _BASE_NOISE
        ),
        "complex": None,
    },
    "corrupt": {
        "vf": f"drawgrid=x=0:y=0:w=iw:h=5:t=1:color=white@0.1,{_BASE_NOISE},eq=gamma=1.5:saturation=0.3:contrast=2",
        "complex": None,
    },
}

VISUAL_PRESETS = set(PRESET_FILTERS.keys())

HELP_TEXT = """\
**I Hate The X — IHTX Bot**
One command, pipe-style syntax:

`g!ihtx effect=value,effect=value,...`

**Visual effects** (value = `true` or N passes):
`chaos` `glitch` `shake` `rainbow` `static` `melt` `corrupt`

**Parameterised effects:**
`huehsv=0.5` — hue shift via hald-CLUT (0–2)
`pinch=1;0.5;0.5;0.5` — strength;radius;cx;cy (all optional)
`reverse=true` — reverse video + audio (applied last)
`hue2=90` — secondary hue rotation in degrees + premultiply blend

**Wave distortion:**
`wave=hs;hf;ha;hp;vs;vf;va;vp;separate;noclip`
  h = horizontal  v = vertical  s=speed f=freq a=amp p=phase
  separate=true — two geq passes instead of one combined
  noclip=true   — drawbox border to suppress edge wrap artifacts
  Example: `wave=1;1;1;0;0.5;1;0.5;0` (horizontal wobble + light vertical)

**Speed:**
`speed=2` — 2x fast forward (also accepts <1 for slow motion)

**Flip:**
`hflip=true` — horizontal flip (left↔right)
`vflip=true` — vertical flip (top↔bottom)

**Mirror:**
`hmirror=1` — left half reflected right (left mirror)
`hmirror=2` — right half reflected left (right mirror)
`vmirror=1` — top half reflected down  (top mirror)
`vmirror=2` — bottom half reflected up (bottom mirror)

**Swirl:**
`swirl=strength;radius;cx;cy;fallout;lock`
  strength — rotation angle in degrees (default 90)
  radius   — 0–1 relative to min(width,height) (default 0.5)
  cx, cy   — center position 0–1 (default 0.5)
  fallout  — `linear` or `quad` (default quad)
  lock     — `true` forces square working area (default false)
  Example: `swirl=180;0.5;0.5;0.5;linear`

**Global options:**
`rep=N` — render cycles (default 1)
`duration=N` — seconds per segment (default 0.5)
`concat=true` — **TRUE IHTX MODE** ✦
  Each rep re-encodes from the *previous* render (artifacts compound).
  All segments joined → total = rep × duration seconds.
  Escalates from slightly degraded → pure chaos.

**Split (zoom into half):**
`split=left` — crop left half, scale to fill frame
`split=right` — crop right half, scale to fill frame
`vsplit=top` — crop top half, scale to fill frame
`vsplit=bottom` — crop bottom half, scale to fill frame

**LUT (colour grade):**
`lut=https://example.com/film.cube` — download a .cube LUT and apply it via lut3d

**Multipitch:**
`multipitch=0;7;12` — semicolon-separated semitones; each is SoX-shifted then amixed
  `sox b.wav out.wav pitch {cents} 25 5 8.5` — blended with `amix=N:normalize=0,highpass=5`

**Examples:**
`g!ihtx chaos=true`
`g!ihtx wave=1;2;1;0;1;1;0.5;0`
`g!ihtx speed=2,hmirror=1`
`g!ihtx glitch=true,concat=true,rep=20,duration=0.5`
`g!ihtx wave=1;1;1,concat=true,rep=15,duration=0.4`
`g!ihtx hmirror=1,vmirror=1,hue2=180`
`g!ihtx hflip=true,swirl=180;0.5;0.5;0.5;linear`
`g!ihtx vflip=true,swirl=90;0.3;0.5;0.5;quad;true`
`g!ihtx multipitch=12` — pitch up one octave
`g!ihtx multipitch=-12;0;12` — multipitch (low + center + high octave mixed)
"""


# ─── Pipe parser ──────────────────────────────────────────────────────────────

def parse_pipe(pipe_str: str) -> list[tuple[str, str]]:
    """
    Parse 'shake=true,glitch=3,pitch=0;7;12' into ordered [(key, raw_val), ...].
    Preserves order for effect sequencing.
    """
    entries = []
    for part in re.split(r"\s*,\s*", pipe_str.strip()):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            key, _, val = part.partition("=")
            entries.append((key.strip().lower(), val.strip()))
        else:
            entries.append((part.lower(), "true"))
    return entries


def is_true(val: str) -> bool:
    return val.strip().lower() in ("true", "1", "yes", "on")


def extract_globals(entries: list[tuple[str, str]]) -> tuple[int, float, bool]:
    """Extract rep, duration, and concat from entries."""
    rep      = 1
    duration = 0.5
    concat   = False
    for k, v in entries:
        if k in ("rep", "repetitions"):
            try:
                rep = max(1, min(MAX_REPETITIONS, int(v)))
            except ValueError:
                pass
        elif k == "duration":
            try:
                duration = max(0.1, min(MAX_DURATION, float(v)))
            except ValueError:
                pass
        elif k == "concat":
            concat = is_true(v)
    return rep, duration, concat


def build_steps(entries: list[tuple[str, str]]) -> list[dict]:
    """Convert ordered entries into step dicts (skip rep/duration/unknown/false)."""
    steps = []
    for key, val in entries:
        if key in ("rep", "repetitions", "duration", "concat"):
            continue

        if key in VISUAL_PRESETS:
            if is_true(val):
                passes = 1
            else:
                try:
                    passes = max(1, min(MAX_REPETITIONS, int(val)))
                except ValueError:
                    continue
            steps.append({"type": "preset", "name": key, "passes": passes})

        elif key == "huehsv":
            try:
                amount = max(0.0, min(2.0, float(val)))
            except ValueError:
                amount = 0.5
            steps.append({"type": "huehsv", "amount": amount})

        elif key == "pinch":
            parts = [p.strip() for p in val.split(";")]
            try:
                strength = float(parts[0]) if len(parts) > 0 else 1.0
                radius   = max(0.01, float(parts[1])) if len(parts) > 1 else 0.5
                cx       = max(0.0, min(1.0, float(parts[2]))) if len(parts) > 2 else 0.5
                cy       = max(0.0, min(1.0, float(parts[3]))) if len(parts) > 3 else 0.5
            except ValueError:
                strength, radius, cx, cy = 1.0, 0.5, 0.5, 0.5
            steps.append({"type": "pinch", "strength": strength, "radius": radius, "cx": cx, "cy": cy})

        elif key in ("multipitch", "soxpitch"):
            raw_parts = [p.strip() for p in val.split(";")]
            semitones = []
            for p in raw_parts:
                try:
                    semitones.append(max(-36.0, min(36.0, float(p))))
                except ValueError:
                    pass
            if semitones:
                steps.append({"type": "multipitch", "semitones": semitones[:8]})

        elif key == "reverse":
            if is_true(val):
                steps.append({"type": "reverse"})

        elif key == "wave":
            p = [x.strip() for x in val.split(";")]
            def _wfp(i, d):
                try: return float(p[i]) if len(p) > i else d
                except ValueError: return d
            def _wbp(i, d=False):
                if len(p) <= i: return d
                return p[i].lower() in ("1","true","t","y","yes","+","on")
            steps.append({
                "type": "wave",
                "hs": _wfp(0, 1.0), "hf": _wfp(1, 1.0), "ha": _wfp(2, 1.0), "hp": _wfp(3, 0.0),
                "vs": _wfp(4, 0.0), "vf": _wfp(5, 0.0), "va": _wfp(6, 0.0), "vp": _wfp(7, 0.0),
                "separate": _wbp(8), "noclip": _wbp(9),
            })

        elif key == "speed":
            try: rate = max(0.01, min(100.0, float(val)))
            except ValueError: rate = 1.0
            steps.append({"type": "speed", "rate": rate})

        elif key == "hmirror":
            try: side = max(1, min(2, int(val)))
            except ValueError: side = 1
            steps.append({"type": "hmirror", "side": side})

        elif key == "vmirror":
            try: side = max(1, min(2, int(val)))
            except ValueError: side = 1
            steps.append({"type": "vmirror", "side": side})

        elif key == "hue2":
            try: degrees = max(-360.0, min(360.0, float(val)))
            except ValueError: degrees = 0.0
            steps.append({"type": "hue2", "degrees": degrees})

        elif key == "hflip":
            if is_true(val):
                steps.append({"type": "hflip"})

        elif key == "vflip":
            if is_true(val):
                steps.append({"type": "vflip"})

        elif key == "split":
            side = val.strip().lower()
            if side in ("left", "right"):
                steps.append({"type": "split", "side": side})

        elif key == "vsplit":
            side = val.strip().lower()
            if side in ("top", "bottom"):
                steps.append({"type": "vsplit", "side": side})

        elif key == "lut":
            url = val.strip()
            if url:
                steps.append({"type": "lut", "url": url})

        elif key == "swirl":
            p = [x.strip() for x in val.split(";")]
            def _swfp(i, d):
                try: return float(p[i]) if len(p) > i else d
                except ValueError: return d
            def _swbp(i, d=False):
                if len(p) <= i: return d
                return p[i].lower() in ("1","true","t","y","yes","+","on")
            _lock = _swbp(5)
            _fall = p[4].lower() if len(p) > 4 else "quad"
            if _fall not in ("linear", "quad"):
                _fall = "quad"
            steps.append({
                "type": "swirl",
                "strength":  _swfp(0, 90.0),
                "radius":    _swfp(1, 0.5),
                "cx":        _swfp(2, 0.5),
                "cy":        _swfp(3, 0.5),
                "fallout":   _fall,
                "lock":      _lock,
            })

    return steps


# ─── Effect builders ──────────────────────────────────────────────────────────

def _build_pinch_vf(strength: float, radius: float, cx: float, cy: float) -> str:
    gauss_arg = (
        f"-3.3333*pow(hypot("
        f"(X-W*{cx})/(W*{radius}),"
        f"(Y-H*{cy})/(H*{radius})"
        f"),2)"
    )
    px = f"W*{cx}+(X-W*{cx})*(1-({strength})*gauss({gauss_arg}))"
    py = f"H*{cy}+(Y-H*{cy})*(1-({strength})*gauss({gauss_arg}))"
    return f"format=yuv444p,geq='p({px},{py})',scale=iw:ih,format=yuv420p"


def _get_video_dims(path: str) -> tuple[int, int]:
    """Return (width, height) of the first video stream; defaults to 640×640."""
    try:
        r = subprocess.run([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0:nk=1", path,
        ], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            w, h = r.stdout.strip().split(",")
            return int(w), int(h)
    except Exception:
        pass
    return 640, 640


def _build_wave_vf(
    src: str,
    hs: float, hf: float, ha: float, hp: float,
    vs: float, vf_: float, va: float, vp: float,
    separate: bool, noclip: bool,
) -> str:
    """
    Sine-wave spatial displacement.
    h_off = sin(T*5*hs + hp*15 + Y/H*PI*hf) * -15*ha   (horizontal shift)
    v_off = sin(T*5*vs + vp*15 + X/W*PI*vf) * -15*va   (vertical shift)
    """
    w, h = _get_video_dims(src)
    h_off = f"(sin((T*5*{hs}+({hp}*15))+(Y/H)*(PI*{hf})))*(-15*{ha})"
    v_off = f"(sin((T*5*{vs}+({vp}*15))+(X/W)*(PI*{vf_})))*(-15*{va})"
    prefix = "drawbox=t=1," if noclip else ""
    if separate:
        geq_str = f"geq='p(X-({h_off}),Y)',geq='p(X,Y-({v_off}))'"
    else:
        geq_str = f"geq='p(X-({h_off}),Y-({v_off}))'"
    return (
        f"{prefix}format=yuv444p,scale=640:640,"
        f"{geq_str},"
        f"scale={w}:{h},setsar=1:1,format=yuv420p"
    )


def _apply_speed(src: str, dst: str, rate: float, is_video: bool, duration: int) -> tuple[bool, str]:
    """
    Change playback rate.
    rate > 1 = fast forward, rate < 1 = slow motion.
    Video: setpt
