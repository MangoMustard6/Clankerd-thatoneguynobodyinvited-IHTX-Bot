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
            with open(CHANNEL_FILE := CHANNEL_BLOCK_FILE) as f:
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

# ─── Runtime stats ──────────────────────────────────────────────────────────[...] 

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

# ─── Pipe parser ──────────────────────────────────────────────────────────��[...]