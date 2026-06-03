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

try:
    from openai import AsyncOpenAI
    _openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY")) if os.environ.get("OPENAI_API_KEY") else None
except ImportError:
    _openai_client = None

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
    Video: setpts=PTS/rate, audio: atempo (chained if rate outside 0.5-2.0).
    Returns (success, error_message).
    """
    try:
        cmd = ["ffmpeg", "-y", "-i", src]
        if is_video:
            vf = f"setpts={1.0/rate}*PTS"
            # Build atempo chain (each filter limited to 0.5–2.0 range)
            af_filters = []
            r = rate
            while r > 2.0:
                af_filters.append("atempo=2.0")
                r /= 2.0
            while r < 0.5:
                af_filters.append("atempo=0.5")
                r /= 0.5
            af_filters.append(f"atempo={r:.6f}")
            af = ",".join(af_filters)
            cmd += ["-vf", vf, "-af", af]
        else:
            # Audio only
            af_filters = []
            r = rate
            while r > 2.0:
                af_filters.append("atempo=2.0")
                r /= 2.0
            while r < 0.5:
                af_filters.append("atempo=0.5")
                r /= 0.5
            af_filters.append(f"atempo={r:.6f}")
            af = ",".join(af_filters)
            cmd += ["-af", af]
        cmd += ["-t", str(duration), dst]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return False, result.stderr[-300:] if result.stderr else "ffmpeg error"
        return True, ""
    except Exception as e:
        return False, str(e)


# ─── Core render engine ───────────────────────────────────────────────────────

async def _run_ffmpeg(cmd: list[str]) -> tuple[bool, str]:
    """Run an ffmpeg command asynchronously."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        return False, stderr.decode(errors="replace")[-400:]
    return True, ""


async def _apply_step(step: dict, src: str, dst: str, is_video: bool, duration: float, tmp_dir: str) -> tuple[bool, str]:
    """Apply a single step to src -> dst. Returns (ok, err)."""
    t = step["type"]

    if t == "preset":
        filt = PRESET_FILTERS[step["name"]]
        for _ in range(step["passes"]):
            tmp = dst + ".pass.mp4"
            if filt["complex"]:
                cmd = ["ffmpeg", "-y", "-i", src, "-filter_complex", f"[0:v]{filt['complex']}[outv]",
                       "-map", "[outv]", "-map", "0:a?", "-c:a", "copy", "-t", str(duration), tmp]
            else:
                cmd = ["ffmpeg", "-y", "-i", src, "-vf", filt["vf"],
                       "-map", "0:v", "-map", "0:a?", "-c:a", "copy", "-t", str(duration), tmp]
            ok, err = await _run_ffmpeg(cmd)
            if not ok:
                return False, err
            os.replace(tmp, dst)
            src = dst
        if src != dst:
            import shutil; shutil.copy2(src, dst)
        return True, ""

    elif t == "huehsv":
        amount = step["amount"]
        vf = f"hue=h={amount * 180}:s={1 + amount}"
        cmd = ["ffmpeg", "-y", "-i", src, "-vf", vf, "-map", "0:v", "-map", "0:a?",
               "-c:a", "copy", "-t", str(duration), dst]
        return await _run_ffmpeg(cmd)

    elif t == "pinch":
        vf = _build_pinch_vf(step["strength"], step["radius"], step["cx"], step["cy"])
        cmd = ["ffmpeg", "-y", "-i", src, "-vf", vf, "-map", "0:v", "-map", "0:a?",
               "-c:a", "copy", "-t", str(duration), dst]
        return await _run_ffmpeg(cmd)

    elif t == "multipitch":
        semitones = step["semitones"]
        # Extract audio, pitch-shift each, amix
        base_wav = os.path.join(tmp_dir, "mp_base.wav")
        cmd_ex = ["ffmpeg", "-y", "-i", src, "-vn", "-ar", "44100", base_wav]
        ok, err = await _run_ffmpeg(cmd_ex)
        if not ok:
            return False, err
        shifted_wavs = []
        for i, semi in enumerate(semitones):
            cents = int(semi * 100)
            out_wav = os.path.join(tmp_dir, f"mp_shift_{i}.wav")
            proc = await asyncio.create_subprocess_exec(
                "sox", base_wav, out_wav, "pitch", str(cents), "25", "5", "8.5",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            if proc.returncode == 0:
                shifted_wavs.append(out_wav)
        if not shifted_wavs:
            import shutil; shutil.copy2(src, dst)
            return True, ""
        # amix shifted wavs
        mixed_wav = os.path.join(tmp_dir, "mp_mixed.wav")
        fc_inputs = "".join(f"[{i}:a]" for i in range(len(shifted_wavs)))
        amix_cmd = ["ffmpeg", "-y"] + sum([["-i", w] for w in shifted_wavs], []) + [
            "-filter_complex", f"{fc_inputs}amix=inputs={len(shifted_wavs)}:normalize=0,highpass=f=5[out]",
            "-map", "[out]", mixed_wav,
        ]
        ok, err = await _run_ffmpeg(amix_cmd)
        if not ok:
            return False, err
        # Merge back with video
        if is_video:
            cmd_merge = ["ffmpeg", "-y", "-i", src, "-i", mixed_wav,
                         "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-t", str(duration), dst]
        else:
            import shutil; shutil.copy2(mixed_wav, dst)
            return True, ""
        return await _run_ffmpeg(cmd_merge)

    elif t == "reverse":
        if is_video:
            cmd = ["ffmpeg", "-y", "-i", src, "-vf", "reverse", "-af", "areverse",
                   "-t", str(duration), dst]
        else:
            cmd = ["ffmpeg", "-y", "-i", src, "-af", "areverse", "-t", str(duration), dst]
        return await _run_ffmpeg(cmd)

    elif t == "wave":
        vf = _build_wave_vf(
            src, step["hs"], step["hf"], step["ha"], step["hp"],
            step["vs"], step["vf"], step["va"], step["vp"],
            step["separate"], step["noclip"],
        )
        cmd = ["ffmpeg", "-y", "-i", src, "-vf", vf, "-map", "0:v", "-map", "0:a?",
               "-c:a", "copy", "-t", str(duration), dst]
        return await _run_ffmpeg(cmd)

    elif t == "speed":
        loop = asyncio.get_event_loop()
        ok, err = await loop.run_in_executor(
            None, _apply_speed, src, dst, step["rate"], is_video, int(duration)
        )
        return ok, err

    elif t == "hmirror":
        side = step["side"]
        if side == 1:
            vf = "crop=iw/2:ih:0:0,scale=iw*2:ih"
        else:
            vf = "crop=iw/2:ih:iw/2:0,scale=iw*2:ih,hflip"
        cmd = ["ffmpeg", "-y", "-i", src, "-vf", vf, "-map", "0:v", "-map", "0:a?",
               "-c:a", "copy", "-t", str(duration), dst]
        return await _run_ffmpeg(cmd)

    elif t == "vmirror":
        side = step["side"]
        if side == 1:
            vf = "crop=iw:ih/2:0:0,scale=iw:ih*2"
        else:
            vf = "crop=iw:ih/2:0:ih/2,scale=iw:ih*2,vflip"
        cmd = ["ffmpeg", "-y", "-i", src, "-vf", vf, "-map", "0:v", "-map", "0:a?",
               "-c:a", "copy", "-t", str(duration), dst]
        return await _run_ffmpeg(cmd)

    elif t == "hue2":
        degrees = step["degrees"]
        vf = f"hue=h={degrees},premultiply"
        cmd = ["ffmpeg", "-y", "-i", src, "-vf", vf, "-map", "0:v", "-map", "0:a?",
               "-c:a", "copy", "-t", str(duration), dst]
        return await _run_ffmpeg(cmd)

    elif t == "hflip":
        cmd = ["ffmpeg", "-y", "-i", src, "-vf", "hflip", "-map", "0:v", "-map", "0:a?",
               "-c:a", "copy", "-t", str(duration), dst]
        return await _run_ffmpeg(cmd)

    elif t == "vflip":
        cmd = ["ffmpeg", "-y", "-i", src, "-vf", "vflip", "-map", "0:v", "-map", "0:a?",
               "-c:a", "copy", "-t", str(duration), dst]
        return await _run_ffmpeg(cmd)

    elif t == "split":
        side = step["side"]
        if side == "left":
            vf = "crop=iw/2:ih:0:0,scale=iw*2:ih"
        else:
            vf = "crop=iw/2:ih:iw/2:0,scale=iw*2:ih"
        cmd = ["ffmpeg", "-y", "-i", src, "-vf", vf, "-map", "0:v", "-map", "0:a?",
               "-c:a", "copy", "-t", str(duration), dst]
        return await _run_ffmpeg(cmd)

    elif t == "vsplit":
        side = step["side"]
        if side == "top":
            vf = "crop=iw:ih/2:0:0,scale=iw:ih*2"
        else:
            vf = "crop=iw:ih/2:0:ih/2,scale=iw:ih*2"
        cmd = ["ffmpeg", "-y", "-i", src, "-vf", vf, "-map", "0:v", "-map", "0:a?",
               "-c:a", "copy", "-t", str(duration), dst]
        return await _run_ffmpeg(cmd)

    elif t == "lut":
        url = step["url"]
        lut_path = os.path.join(tmp_dir, "custom.cube")
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status != 200:
                        return False, f"LUT download failed: HTTP {r.status}"
                    with open(lut_path, "wb") as f:
                        f.write(await r.read())
        except Exception as e:
            return False, f"LUT download error: {e}"
        vf = f"lut3d=file='{lut_path}'"
        cmd = ["ffmpeg", "-y", "-i", src, "-vf", vf, "-map", "0:v", "-map", "0:a?",
               "-c:a", "copy", "-t", str(duration), dst]
        return await _run_ffmpeg(cmd)

    elif t == "swirl":
        strength = step["strength"]
        radius   = step["radius"]
        cx       = step["cx"]
        cy       = step["cy"]
        fallout  = step["fallout"]
        lock     = step["lock"]
        w, h = _get_video_dims(src)
        size = min(w, h) if lock else max(w, h)
        r_px = radius * size
        # Build swirl geq expression
        if fallout == "linear":
            weight = f"clip(1-hypot((X-W*{cx})/({r_px}),(Y-H*{cy})/({r_px})),0,1)"
        else:
            weight = f"clip(1-pow(hypot((X-W*{cx})/({r_px}),(Y-H*{cy})/({r_px})),2),0,1)"
        angle_expr = f"{strength}*PI/180*{weight}"
        px = f"W*{cx}+cos({angle_expr})*(X-W*{cx})-sin({angle_expr})*(Y-H*{cy})"
        py = f"H*{cy}+sin({angle_expr})*(X-W*{cx})+cos({angle_expr})*(Y-H*{cy})"
        vf = f"format=yuv444p,geq='p({px},{py})',scale=iw:ih,format=yuv420p"
        cmd = ["ffmpeg", "-y", "-i", src, "-vf", vf, "-map", "0:v", "-map", "0:a?",
               "-c:a", "copy", "-t", str(duration), dst]
        return await _run_ffmpeg(cmd)

    # Unknown step — pass through
    import shutil
    shutil.copy2(src, dst)
    return True, ""


async def _render(src: str, steps: list[dict], duration: float, is_video: bool, concat: bool, rep: int, tmp_dir: str) -> tuple[bool, str, str]:
    """
    Run all steps over rep repetitions.
    concat=True: each rep re-encodes from previous output, then segments are concatenated.
    concat=False: steps applied sequentially, repeated rep times, only final result returned.
    Returns (ok, output_path, error).
    """
    segments = []
    current = src

    for rep_i in range(rep):
        step_src = current
        for si, step in enumerate(steps):
            step_dst = os.path.join(tmp_dir, f"rep{rep_i}_step{si}.mp4")
            ok, err = await _apply_step(step, step_src, step_dst, is_video, duration, tmp_dir)
            if not ok:
                return False, "", err
            step_src = step_dst
        seg_path = os.path.join(tmp_dir, f"seg_{rep_i}.mp4")
        os.replace(step_src, seg_path)
        segments.append(seg_path)
        if concat:
            current = seg_path  # next rep starts from this degraded output
        else:
            current = src  # repeat from original

    if concat and len(segments) > 1:
        # Concatenate all segments
        list_file = os.path.join(tmp_dir, "concat_list.txt")
        with open(list_file, "w") as f:
            for seg in segments:
                f.write(f"file '{seg}'\n")
        final = os.path.join(tmp_dir, "final.mp4")
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
               "-c", "copy", final]
        ok, err = await _run_ffmpeg(cmd)
        if not ok:
            return False, "", err
        return True, final, ""
    else:
        return True, segments[-1], ""


# ─── Attachment helpers ────────────────────────────────────────────────────────

async def _download_attachment(attachment: discord.Attachment, tmp_dir: str) -> tuple[bool, str, str]:
    """Download attachment to tmp_dir. Returns (ok, path, err)."""
    ext = Path(attachment.filename).suffix.lower()
    dst = os.path.join(tmp_dir, f"input{ext}")
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(attachment.url, timeout=aiohttp.ClientTimeout(total=60)) as r:
                if r.status != 200:
                    return False, "", f"Download failed: HTTP {r.status}"
                with open(dst, "wb") as f:
                    f.write(await r.read())
        return True, dst, ""
    except Exception as e:
        return False, "", str(e)


def _get_attachment(ctx: commands.Context) -> discord.Attachment | None:
    """Get the first supported attachment from the message or its reference."""
    for att in ctx.message.attachments:
        if Path(att.filename).suffix.lower() in SUPPORTED_EXTENSIONS:
            return att
    if ctx.message.reference and ctx.message.reference.resolved:
        ref = ctx.message.reference.resolved
        if isinstance(ref, discord.Message):
            for att in ref.attachments:
                if Path(att.filename).suffix.lower() in SUPPORTED_EXTENSIONS:
                    return att
    return None


# ─── Discord commands ──────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})", flush=True)


@bot.command(name="ihtx", aliases=["effect", "destroy"])
async def cmd_ihtx(ctx: commands.Context, *, pipe_str: str = ""):
    global _renders_completed, _renders_in_progress
    att = _get_attachment(ctx)
    if not att:
        await ctx.reply("❌ Attach or reply to a supported media file.\nSupported: " + ", ".join(sorted(SUPPORTED_EXTENSIONS)))
        return
    if att.size > MAX_FILE_SIZE:
        await ctx.reply(f"❌ File too large (max {MAX_FILE_SIZE // 1024 // 1024} MB).")
        return
    if not pipe_str.strip():
        await ctx.reply(f"❌ No effects specified.\n{HELP_TEXT[:1800]}")
        return

    entries = parse_pipe(pipe_str)
    rep, duration, concat = extract_globals(entries)
    steps = build_steps(entries)
    if not steps:
        await ctx.reply("❌ No valid effects found. Use `g!help_ihtx` for options.")
        return

    ext = Path(att.filename).suffix.lower()
    is_video = ext in VIDEO_EXTENSIONS

    status_msg = await ctx.reply("⚙️ Processing…")
    _renders_in_progress += 1
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            ok, src, err = await _download_attachment(att, tmp_dir)
            if not ok:
                await status_msg.edit(content=f"❌ Download failed: {err}")
                return
            ok, out_path, err = await _render(src, steps, duration, is_video, concat, rep, tmp_dir)
            if not ok:
                await status_msg.edit(content=f"❌ Render failed: {err[:300]}")
                return
            out_size = os.path.getsize(out_path)
            if out_size > MAX_FILE_SIZE:
                await status_msg.edit(content=f"❌ Output too large ({out_size // 1024 // 1024} MB > 25 MB). Try fewer reps or shorter duration.")
                return
            out_name = f"ihtx_{Path(att.filename).stem}.mp4"
            await status_msg.delete()
            await ctx.reply(file=discord.File(out_path, filename=out_name))
            _renders_completed += 1
    except Exception as e:
        await status_msg.edit(content=f"❌ Unexpected error: {e}")
    finally:
        _renders_in_progress -= 1


@bot.command(name="help_ihtx", aliases=["ihtxhelp", "ihelp"])
async def cmd_help_ihtx(ctx: commands.Context):
    # Split help text to stay within Discord's 2000-char limit
    chunks = []
    current = ""
    for line in HELP_TEXT.splitlines(keepends=True):
        if len(current) + len(line) > 1900:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    for chunk in chunks:
        await ctx.send(chunk)


@bot.command(name="stats")
async def cmd_stats(ctx: commands.Context):
    uptime = datetime.timedelta(seconds=int(time.time() - _bot_start_time))
    await ctx.reply(
        f"📊 **IHTX Bot Stats**\n"
        f"Uptime: {uptime}\n"
        f"Renders completed: {_renders_completed}\n"
        f"Renders in progress: {_renders_in_progress}"
    )


@bot.command(name="ping")
async def cmd_ping(ctx: commands.Context):
    latency_ms = round(bot.latency * 1000)
    await ctx.reply(f"🏓 Pong! `{latency_ms}ms`")


# ─── Owner-only commands ───────────────────────────────────────────────────────

@bot.command(name="owner")
async def cmd_owner(ctx: commands.Context, action: str = "", user: discord.User | None = None):
    if not _is_owner(ctx):
        return
    if action == "add" and user:
        owner_ids.add(user.id)
        _save_owner_ids()
        await ctx.reply(f"✅ Added {user} as owner.")
    elif action == "remove" and user:
        owner_ids.discard(user.id)
        _save_owner_ids()
        await ctx.reply(f"✅ Removed {user} from owners.")
    elif action == "list":
        await ctx.reply(f"Owners: {', '.join(str(i) for i in owner_ids)}")
    else:
        await ctx.reply("Usage: `g!owner add/remove/list @user`")


@bot.command(name="block")
async def cmd_block(ctx: commands.Context, user: discord.User | None = None):
    if not _is_owner(ctx):
        return
    if not user:
        await ctx.reply("Usage: `g!block @user`")
        return
    blocklist.add(user.id)
    _save_blocklist()
    await ctx.reply(f"✅ Blocked {user}.")


@bot.command(name="unblock")
async def cmd_unblock(ctx: commands.Context, user: discord.User | None = None):
    if not _is_owner(ctx):
        return
    if not user:
        await ctx.reply("Usage: `g!unblock @user`")
        return
    blocklist.discard(user.id)
    _save_blocklist()
    await ctx.reply(f"✅ Unblocked {user}.")


@bot.command(name="blockchannel")
async def cmd_blockchannel(ctx: commands.Context):
    if not _is_owner(ctx):
        return
    channel_blocks.add(ctx.channel.id)
    _save_channel_blocks()
    await ctx.reply("✅ This channel is now blocked.")


@bot.command(name="unblockchannel")
async def cmd_unblockchannel(ctx: commands.Context):
    if not _is_owner(ctx):
        return
    channel_blocks.discard(ctx.channel.id)
    _save_channel_blocks()
    await ctx.reply("✅ This channel is now unblocked.")


@bot.command(name="setlimit")
async def cmd_setlimit(ctx: commands.Context, user: discord.User | None = None, limit: int = HEAVY_LIMIT_DEFAULT):
    if not _is_owner(ctx):
        return
    if not user:
        await ctx.reply("Usage: `g!setlimit @user N`")
        return
    heavy_limits[user.id] = max(0, limit)
    _save_limits()
    await ctx.reply(f"✅ Set daily limit for {user} to {limit}.")


@bot.command(name="tag")
async def cmd_tag(ctx: commands.Context, action: str = "", name: str = "", *, pipe_str: str = ""):
    if action == "save":
        if not name or not pipe_str:
            await ctx.reply("Usage: `g!tag save <name> <effect pipe>`")
            return
        tags[name] = {"pipe": pipe_str, "author": ctx.author.id}
        _save_tags()
        await ctx.reply(f"✅ Tag `{name}` saved.")
    elif action == "use":
        if name not in tags:
            await ctx.reply(f"❌ Tag `{name}` not found.")
            return
        att = _get_attachment(ctx)
        if not att:
            await ctx.reply("❌ Attach or reply to a supported media file.")
            return
        # Delegate to ihtx with the tag's pipe
        fake_ctx = ctx
        await cmd_ihtx(fake_ctx, pipe_str=tags[name]["pipe"])
    elif action == "list":
        if not tags:
            await ctx.reply("No tags saved yet.")
        else:
            lines = [f"`{n}`: {v['pipe']}" for n, v in list(tags.items())[:20]]
            await ctx.reply("\n".join(lines))
    elif action == "delete":
        if not _is_owner(ctx) and tags.get(name, {}).get("author") != ctx.author.id:
            await ctx.reply("❌ You can only delete your own tags.")
            return
        tags.pop(name, None)
        _save_tags()
        await ctx.reply(f"✅ Tag `{name}` deleted.")
    else:
        await ctx.reply("Usage: `g!tag save/use/list/delete <name> [pipe]`")


@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply(f"❌ Missing argument: `{error.param.name}`")
        return
    print(f"Command error in {ctx.command}: {error}", flush=True)


# ─── Autoreply ────────────────────────────────────────────────────────────────

AUTOREPLY_FILE = Path("bot/autoreply.json")

# channel_id -> {"enabled": bool, "prompt": str, "history": [...]}
autoreply_state: dict[int, dict] = {}

DEFAULT_AUTOREPLY_PROMPT = (
    "You are a witty and helpful Discord bot named Glossi. "
    "Reply concisely in 1–3 sentences. Match the tone of the conversation."
)

def _load_autoreply():
    global autoreply_state
    if AUTOREPLY_FILE.exists():
        try:
            raw = json.loads(AUTOREPLY_FILE.read_text())
            autoreply_state = {int(k): v for k, v in raw.items()}
        except Exception:
            autoreply_state = {}

def _save_autoreply():
    AUTOREPLY_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTOREPLY_FILE.write_text(json.dumps(
        {str(k): {**v, "history": v.get("history", [])} for k, v in autoreply_state.items()},
        indent=2
    ))

_load_autoreply()


@bot.command(name="autoreply")
async def cmd_autoreply(ctx: commands.Context, action: str = "", *, rest: str = ""):
    """
    g!autoreply on          — enable autoreply in this channel
    g!autoreply off         — disable autoreply in this channel
    g!autoreply prompt <..> — set a custom system prompt for this channel
    g!autoreply reset       — reset prompt to default and clear history
    g!autoreply status      — show current settings for this channel
    """
    if not _is_owner(ctx):
        await ctx.reply("❌ Only owners can configure autoreply.")
        return

    cid = ctx.channel.id
    state = autoreply_state.setdefault(cid, {"enabled": False, "prompt": DEFAULT_AUTOREPLY_PROMPT, "history": []})

    if action == "on":
        if _openai_client is None:
            await ctx.reply("❌ OpenAI is not available. Check that `OPENAI_API_KEY` is set.")
            return
        state["enabled"] = True
        _save_autoreply()
        await ctx.reply("✅ Autoreply **enabled** in this channel. I'll reply to every message.")

    elif action == "off":
        state["enabled"] = False
        _save_autoreply()
        await ctx.reply("✅ Autoreply **disabled** in this channel.")

    elif action == "prompt":
        if not rest.strip():
            await ctx.reply("Usage: `g!autoreply prompt <your system prompt>`")
            return
        state["prompt"] = rest.strip()
        state["history"] = []
        _save_autoreply()
        await ctx.reply(f"✅ System prompt updated and history cleared.\n> {rest.strip()[:200]}")

    elif action == "reset":
        state["prompt"] = DEFAULT_AUTOREPLY_PROMPT
        state["history"] = []
        _save_autoreply()
        await ctx.reply("✅ Prompt reset to default and history cleared.")

    elif action == "status":
        enabled = "✅ On" if state["enabled"] else "❌ Off"
        prompt_preview = state.get("prompt", DEFAULT_AUTOREPLY_PROMPT)[:120]
        history_len = len(state.get("history", []))
        await ctx.reply(
            f"**Autoreply status for this channel**\n"
            f"State: {enabled}\n"
            f"History turns: {history_len // 2}\n"
            f"Prompt: `{prompt_preview}{'…' if len(state.get('prompt','')) > 120 else ''}`"
        )

    else:
        await ctx.reply(
            "**Autoreply commands:**\n"
            "`g!autoreply on` — enable in this channel\n"
            "`g!autoreply off` — disable in this channel\n"
            "`g!autoreply prompt <text>` — set custom system prompt\n"
            "`g!autoreply reset` — reset prompt & clear history\n"
            "`g!autoreply status` — show current settings"
        )


@bot.event
async def on_message(message: discord.Message):
    # Always process commands first
    await bot.process_commands(message)

    # Ignore the bot's own messages and commands
    if message.author.bot:
        return
    if message.content.startswith(bot.command_prefix):
        return

    # Check if autoreply is enabled in this channel
    state = autoreply_state.get(message.channel.id)
    if not state or not state.get("enabled"):
        return
    if _openai_client is None:
        return

    # Build conversation history (keep last 20 turns = 40 messages)
    history: list[dict] = state.setdefault("history", [])
    history.append({"role": "user", "content": f"{message.author.display_name}: {message.content}"})
    if len(history) > 40:
        history[:] = history[-40:]

    try:
        async with message.channel.typing():
            response = await _openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": state.get("prompt", DEFAULT_AUTOREPLY_PROMPT)},
                    *history,
                ],
                max_tokens=256,
            )
        reply_text = response.choices[0].message.content or "…"
        history.append({"role": "assistant", "content": reply_text})
        _save_autoreply()
        await message.reply(reply_text)
    except Exception as e:
        err = str(e)
        print(f"Autoreply error: {err}", flush=True)
        if "insufficient_quota" in err or "quota" in err.lower():
            await message.reply("❌ Autoreply is out of OpenAI credits. Top up at <https://platform.openai.com/settings/billing> or disable with `g!autoreply off`.")
        elif "api_key" in err.lower() or "auth" in err.lower() or "401" in err:
            await message.reply("❌ Autoreply error: invalid API key. Use `g!autoreply off` to disable.")


# ─── Entry point ───────────────────────────────────────────────────────────────

bot.run(TOKEN)
