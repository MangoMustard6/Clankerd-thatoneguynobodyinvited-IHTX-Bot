"""
IHTX Bot — I Hate The X FFmpeg Discord Bot

Full implementation with preset effects, custom effect chaining (g!ihtx),
and the preview1280 TV-simulator montage command.

Dependencies required at runtime: ffmpeg, aiohttp, discord.py, optionally yt-dlp,
ImageMagick/sox/etc. depending on advanced effects.
"""

import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import json
import math
import os
import re
import shlex
import tempfile
import shutil
import subprocess
import aiohttp
import sys
import time
from pathlib import Path

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

# ---------- Configuration & constants ----------

TOKEN = os.environ.get("DISCORD_TOKEN")

# Default owner (can be extended via owner file)
OWNER_ID = 1355759019330895973

OWNER_IDS_FILE = Path("bot/owner_ids.json")
owner_ids: set[int] = {OWNER_ID}


def _load_owner_ids():
    global owner_ids
    try:
        if OWNER_IDS_FILE.exists():
            with OWNER_IDS_FILE.open() as f:
                owner_ids = set(int(x) for x in json.load(f))
        else:
            owner_ids = {OWNER_ID}
    except Exception:
        owner_ids = {OWNER_ID}


def _save_owner_ids():
    OWNER_IDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OWNER_IDS_FILE.open("w") as f:
        json.dump(list(owner_ids), f)


def _is_owner(ctx: commands.Context) -> bool:
    return ctx.author.id in owner_ids


def _is_owner_by_id(user_id: int) -> bool:
    return user_id in owner_ids

_load_owner_ids()

# Heavy command rate limiting
HEAVY_COMMANDS = {"ihtx", "effect", "destroy", "preview1280", "p1280", "multipitch", "mp", "multi", "tvsim", "tv", "ihtxsync", "download", "dl"}
HEAVY_LIMIT_DEFAULT = 10
HEAVY_LIMIT_OWNER = 5340
LIMITS_FILE = Path("bot/limits.json")
heavy_limits: dict[int, int] = {}
heavy_usage: dict[int, list[float]] = {}


def _load_limits():
    global heavy_limits
    try:
        if LIMITS_FILE.exists():
            with LIMITS_FILE.open() as f:
                heavy_limits = {int(k): int(v) for k, v in json.load(f).items()}
        else:
            heavy_limits = {}
    except Exception:
        heavy_limits = {}


def _save_limits():
    LIMITS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LIMITS_FILE.open("w") as f:
        json.dump(heavy_limits, f)


def _check_heavy_limit(user_id: int) -> tuple[bool, str]:
    if _is_owner_by_id(user_id):
        return True, ""
    limit = heavy_limits.get(user_id, HEAVY_LIMIT_DEFAULT)
    now = time.time()
    day_ago = now - 86400
    usage = [t for t in heavy_usage.get(user_id, []) if t > day_ago]
    heavy_usage[user_id] = usage
    if len(usage) >= limit:
        return False, f"Heavy command limit reached ({limit}/{limit} per 24h). Contact an owner."
    usage.append(now)
    return True, ""

_load_limits()

# Blocklist (users)
BLOCKLIST_FILE = Path("bot/blocklist.json")
blocklist: set[int] = set()


def _load_blocklist():
    global blocklist
    try:
        if BLOCKLIST_FILE.exists():
            with BLOCKLIST_FILE.open() as f:
                blocklist = set(int(x) for x in json.load(f))
        else:
            blocklist = set()
    except Exception:
        blocklist = set()


def _save_blocklist():
    BLOCKLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    with BLOCKLIST_FILE.open("w") as f:
        json.dump(list(blocklist), f)

_load_blocklist()

# Channel blocklist
CHANNEL_BLOCK_FILE = Path("bot/channel_blocks.json")
channel_blocks: set[int] = set()


def _load_channel_blocks():
    global channel_blocks
    try:
        if CHANNEL_BLOCK_FILE.exists():
            with CHANNEL_BLOCK_FILE.open() as f:
                channel_blocks = set(int(x) for x in json.load(f))
        else:
            channel_blocks = set()
    except Exception:
        channel_blocks = set()


def _save_channel_blocks():
    CHANNEL_BLOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with CHANNEL_BLOCK_FILE.open("w") as f:
        json.dump(list(channel_blocks), f)

_load_channel_blocks()

# Tags (custom presets)
TAGS_FILE = Path("bot/tags.json")
tags: dict[str, dict] = {}


def _load_tags():
    global tags
    try:
        if TAGS_FILE.exists():
            with TAGS_FILE.open() as f:
                tags = json.load(f)
        else:
            tags = {}
    except Exception:
        tags = {}


def _save_tags():
    TAGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with TAGS_FILE.open("w") as f:
        json.dump(tags, f, indent=2)

_load_tags()

# Intents and bot
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="g!", intents=intents)

# Runtime stats
_bot_start_time: float = time.time()
_renders_completed: int = 0
_renders_in_progress: int = 0

# File handling constants
SUPPORTED_EXTENSIONS  = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".gif", ".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXTENSIONS      = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".gif"}
AUDIO_VIDEO_EXTS      = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
MAX_FILE_SIZE         = 25 * 1024 * 1024
MAX_REPETITIONS       = 100
MAX_DURATION          = 600

# Effect filter definitions
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

(Full help included in repository's README/help text.)
"""

# ---------- Global checks ----------

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

# ---------- Helpers: download and ffmpeg ----------

async def download_attachment(attachment: discord.Attachment, dest: str):
    """Download a discord.Attachment to path `dest`."""
    async with aiohttp.ClientSession() as session:
        async with session.get(attachment.url) as resp:
            if resp.status != 200:
                raise ValueError(f"Failed to download attachment (HTTP {resp.status})")
            data = await resp.read()
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)


async def download_url(url: str, dest: str):
    """Download an arbitrary URL to path `dest`."""
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise ValueError(f"Failed to download URL (HTTP {resp.status})")
            data = await resp.read()
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)


def _ffprobe(input_path: str, *args: str) -> str:
    """Run ffprobe and return stripped stdout."""
    cmd = ["ffprobe", "-v", "error"] + list(args) + [input_path]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return result.stdout.strip()


def _ffprobe_duration(input_path: str) -> float:
    """Get duration in seconds."""
    out = _ffprobe(input_path, "-show_entries", "format=duration",
                   "-of", "csv=p=0")
    try:
        return float(out)
    except (ValueError, TypeError):
        return 0.0


def _ffprobe_video_info(input_path: str) -> dict:
    """Return width, height, duration, nb_frames, r_frame_rate."""
    info = {"width": 0, "height": 0, "duration": 0.0,
            "nb_frames": 0, "r_frame_rate": "30"}
    w = _ffprobe(input_path, "-select_streams", "v:0",
                 "-show_entries", "stream=width",
                 "-of", "default=nw=1:nk=1")
    h = _ffprobe(input_path, "-select_streams", "v:0",
                 "-show_entries", "stream=height",
                 "-of", "default=nw=1:nk=1")
    fc = _ffprobe(input_path, "-select_streams", "v:0",
                  "-show_entries", "stream=nb_frames",
                  "-of", "default=nokey=1:noprint_wrappers=1")
    fr = _ffprobe(input_path, "-select_streams", "v:0",
                  "-show_entries", "stream=r_frame_rate",
                  "-of", "default=nokey=1:noprint_wrappers=1")
    dur = _ffprobe_duration(input_path)
    try:
        info["width"] = int(w)
    except (ValueError, TypeError):
        pass
    try:
        info["height"] = int(h)
    except (ValueError, TypeError):
        pass
    try:
        info["nb_frames"] = int(fc)
    except (ValueError, TypeError):
        pass
    if fr:
        info["r_frame_rate"] = fr
    info["duration"] = dur
    return info


def _ffprobe_sample_rate(input_path: str) -> int:
    """Return the audio sample rate of the input file, defaulting to 44100."""
    sr = _ffprobe(input_path, "-select_streams", "a:0",
                  "-show_entries", "stream=sample_rate",
                  "-of", "default=nw=1:nk=1")
    try:
        return int(sr)
    except (ValueError, TypeError):
        return 44100


def _run_ffmpeg_raw(cmd: list[str], timeout: int = 180) -> tuple[bool, str]:
    """Run an arbitrary ffmpeg command. Returns (ok, stderr-or-empty)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            return False, result.stderr[-2000:]
        return True, ""
    except subprocess.TimeoutExpired:
        return False, f"FFmpeg timed out (>{timeout}s)"
    except Exception as e:
        return False, str(e)


def run_ffmpeg(input_path: str, output_path: str, preset: str, is_video: bool) -> tuple[bool, str]:
    """Run ffmpeg using PRESET_FILTERS. Returns (ok, stderr-or-empty)."""
    cfg = PRESET_FILTERS.get(preset)
    if cfg is None:
        cfg = PRESET_FILTERS["chaos"]

    if is_video:
        if cfg["complex"]:
            cmd = [
                "ffmpeg", "-y", "-i", input_path,
                "-filter_complex", cfg["complex"],
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "copy",
                "-t", "30",
                output_path
            ]
        else:
            cmd = [
                "ffmpeg", "-y", "-i", input_path,
                "-vf", cfg["vf"],
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "copy",
                "-t", "30",
                output_path
            ]
    else:
        # Image → animated GIF
        if cfg["complex"]:
            fc = cfg["complex"] + ",split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"
            cmd = [
                "ffmpeg", "-y",
                "-loop", "1", "-i", input_path,
                "-filter_complex", fc,
                "-t", "3",
                output_path
            ]
        else:
            vf = cfg["vf"] + ",split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"
            cmd = [
                "ffmpeg", "-y",
                "-loop", "1", "-i", input_path,
                "-vf", vf,
                "-t", "3",
                output_path
            ]

    return _run_ffmpeg_raw(cmd)


def get_output_ext(input_ext: str, is_video: bool) -> str:
    return ".mp4" if is_video else ".gif"

# ---------- HueHSV (ImageMagick haldclut) ----------

def _run_huehsv(
    input_path: str,
    output_path: str,
    hue: float,
) -> tuple[bool, str]:
    """Apply huehsv using ImageMagick haldclut + FFmpeg haldclut filter.

    Uses: magick hald:6 -modulate 100,100,<hue*200+100> hsv.ppm
    Then: ffmpeg -i input -vf "movie=hsv.ppm,[in]haldclut,format=rgba" -pix_fmt yuv420p output
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        hald_path = os.path.join(tmpdir, "hsv.ppm")
        modulate_val = hue * 200 + 100
        # Generate hald clut using ImageMagick
        cmd = ["magick", "hald:6", "-modulate", f"100,100,{modulate_val}", hald_path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return False, f"Haldclut generation failed: {result.stderr}"

        # Apply via FFmpeg haldclut filter
        ok, err = _run_ffmpeg_raw([
            "ffmpeg", "-y", "-i", input_path,
            "-vf", f"movie={hald_path},[in]haldclut,format=rgba",
            "-pix_fmt", "yuv420p",
            output_path,
        ], timeout=180)
        if not ok:
            return False, f"FFmpeg haldclut failed: {err}"

        return True, ""



# ---------- Pipe effects engine ----------

def _parse_pipe_effects(pipe_str: str) -> list[tuple[str, list[str]]]:
    """Parse semicolon-separated pipe effects.

    Each effect is: name=value or name value.
    Params are semicolon-separated within each effect.
    """
    effects = []
    for part in pipe_str.split(";"):
        part = part.strip()
        if not part:
            continue
        # Strip optional annotations like (magick)
        part = re.sub(r"\s*\(magick\)\s*", "", part, flags=re.IGNORECASE)
        if "=" in part:
            name, value = part.split("=", 1)
            name = name.strip().lower()
            params = [p.strip() for p in value.split(";") if p.strip()]
        else:
            # Check if there's a space-separated value (e.g. "huehsv 0.5")
            tokens = part.split(None, 1)
            name = tokens[0].strip().lower()
            params = []
            if len(tokens) > 1:
                params = [p.strip() for p in tokens[1].split(";") if p.strip()]
        effects.append((name, params))
    return effects


def _build_ffmpeg_pipe_vf(name: str, params: list[str]) -> str | None:
    """Build a single FFmpeg -vf filter string for a pipe effect."""
    if name == "hflip":
        return "hflip"
    if name == "vflip":
        return "vflip"
    if name in ("invert", "negate"):
        return "negate"
    if name == "grayscale":
        return "colorchannelmixer=.299:.587:.114:0:.299:.587:.114:0:.299:.587:.114"
    if name == "sepia":
        return "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131"
    if name == "rotate":
        angle = params[0] if params else "0"
        return f"rotate={angle}/180*PI"
    if name == "ccshue":
        val = params[0] if params else "0"
        return f"hue=h={val}"
    if name == "brightness":
        val = params[0] if params else "0"
        return f"eq=brightness={val}"
    if name == "contrast":
        val = params[0] if params else "1"
        return f"eq=contrast={val}"
    if name == "saturation":
        val = params[0] if params else "1"
        return f"hue=s={val}"
    if name == "swapuv":
        return "swapuv"
    if name == "mirror":
        angle = params[0] if params else "0"
        a_val = float(angle)
        mirror_geq = "p(W*0.5-abs(X-W*0.5),Y)"
        if a_val == 0:
            return f"format=yuv444p,geq='{mirror_geq}',format=yuv420p"
        a_plus_90 = a_val + 90
        return (
            f"format=yuv444p,"
            f"rotate={a_plus_90}/180*PI:iw*2:ih*2,"
            f"geq='{mirror_geq}',"
            f"rotate=-{a_plus_90}/180*PI,"
            f"crop=iw/2:ih/2,"
            f"format=yuv420p"
        )
    if name == "zoom":
        amount = params[0] if params else "1.1"
        zoom_geq = f"p((W/2)+(X-(W/2))/{amount},(H/2)+(Y-(H/2))/{amount})"
        return (
            f"format=yuv444p,rotate=0:iw*1.1:ih*1.1,"
            f"geq='{zoom_geq}',"
            f"scale=iw:ih,crop=iw:ih,format=yuv420p"
        )
    if name in ("pinch&punch", "p&p", "pinchpunch"):
        strength = params[0] if len(params) > 0 else "1"
        radius = params[1] if len(params) > 1 else "0.5"
        cx = params[2] if len(params) > 2 else "0.5"
        cy = params[3] if len(params) > 3 else "0.5"
        geq_expr = (
            f"p(W*{cx}+(X-W*{cx})*(1-({strength})*gauss(-3.3333*pow(hypot((X-W*{cx})/(W*{radius}),(Y-H*{cy})/(H*{radius})),2))),"
            f"H*{cy}+(Y-H*{cy})*(1-({strength})*gauss(-3.3333*pow(hypot((X-W*{cx})/(W*{radius}),(Y-H*{cy})/(H*{radius})),2))))"
        )
        return f"format=yuv444p,geq='{geq_expr}',scale=iw:ih,format=yuv420p"
    if name == "swirl":
        angle = params[0] if len(params) > 0 else "180"
        radius = params[1] if len(params) > 1 else "0.5"
        cx = params[2] if len(params) > 2 else "0.5"
        cy = params[3] if len(params) > 3 else "0.5"
        fallout = params[4] if len(params) > 4 else "quad"
        lockaspectratio = params[5] if len(params) > 5 else "false"
        exp_str = "" if fallout == "linear" else "^2"
        min_wh = "min(W,H)"
        swirl_geq = (
            f"p(W*{cx}+(hypot(X-W*{cx},Y-H*{cy})+1e-6)*cos((atan2(Y-H*{cy},X-W*{cx}))+(({angle})/180*PI)*(if(lt(hypot(X-W*{cx},Y-H*{cy})+1e-6,{min_wh}*{radius}),1-(hypot(X-W*{cx},Y-H*{cy})+1e-6)/({min_wh}*{radius}),0){exp_str})),"
            f"H*{cy}+(hypot(X-W*{cx},Y-H*{cy})+1e-6)*sin((atan2(Y-H*{cy},X-W*{cx}))+(({angle})/180*PI)*(if(lt(hypot(X-W*{cx},Y-H*{cy})+1e-6,{min_wh}*{radius}),1-(hypot(X-W*{cx},Y-H*{cy})+1e-6)/({min_wh}*{radius}),0){exp_str})))"
        )
        if lockaspectratio.lower() in ("1", "true", "t", "y", "yes", "+", "on"):
            return f"format=yuv444p,scale=ih:ih,geq='{swirl_geq}',scale=iw:ih,setsar=1:1,format=yuv420p"
        return f"format=yuv444p,geq='{swirl_geq}',scale=iw:ih,format=yuv420p"
    if name == "gm91deform":
        deform_geq = (
            "p((W/2)+((X-W/2)/lerp(1,asin(sin(-Y/H)),0.164))/1.22"
            "+((Y-H/2)*(-0.136))+((0.047*W)*pow((Y-H/2)/(H/2),2))+(-W/40)"
            ",(H/2)+((Y-H/2)/1.27)/lerp(1,sin((X/W)*PI),0.12)"
            "-(((0.014)*H)*pow((X-W/2)/(W/2),2))+((X-W/2)*(0.12))-(1.2))"
        )
        return (
            f"format=yuv444p,scale=360:360,setsar=1:1,rotate=0:iw*1.05:ih*1.05,"
            f"geq='{deform_geq}',"
            f"scale=640*1.05:360*1.05,crop=640:360:(in_w-in_h)/2+8,scale=iw:ih,setsar=1,format=yuv420p"
        )
    if name == "realgm4":
        return "curves=r='0/0 0.5/0.75 1/0':g='0/0 0.5/0.75 1/0':b='0/0 0.5/0.75 1/0',format=yuv420p"
    if name == "invertrgb":
        r_inv = params[0] if len(params) > 0 else "1"
        g_inv = params[1] if len(params) > 1 else "0"
        b_inv = params[2] if len(params) > 2 else "0"
        r_curve = "0/1 1/0" if r_inv == "1" else "0/0 1/1"
        g_curve = "0/1 1/0" if g_inv == "1" else "0/0 1/1"
        b_curve = "0/1 1/0" if b_inv == "1" else "0/0 1/1"
        return f"curves=r='{r_curve}':g='{g_curve}':b='{b_curve}'"
    if name == "invlum":
        return "curves=all='0/1 1/0'"
    if name == "volume":
        val = params[0] if params else "1"
        return f"volume={val}"
    if name == "vibrato":
        freq = params[0] if len(params) > 0 else "5"
        depth = params[1] if len(params) > 1 else "0.5"
        return f"vibrato=f={freq}:d={depth}"
    if name == "areverse":
        return "areverse"
    if name == "channelblend":
        r = params[0] if len(params) > 0 else "r"
        g = params[1] if len(params) > 1 else "g"
        b = params[2] if len(params) > 2 else "b"
        ch_map = {"r": "1:0:0", "g": "0:1:0", "b": "0:0:1"}
        rr = ch_map.get(r, "1:0:0")
        gg = ch_map.get(g, "0:1:0")
        bb = ch_map.get(b, "0:0:1")
        return (
            f"colorchannelmixer=rr={rr.split(':')[0]}:rg={rr.split(':')[1]}:rb={rr.split(':')[2]}"
            f":gr={gg.split(':')[0]}:gg={gg.split(':')[1]}:gb={gg.split(':')[2]}"
            f":br={bb.split(':')[0]}:bg={bb.split(':')[1]}:bb={bb.split(':')[2]}"
        )
    return None


def _apply_pipe_effects(
    input_path: str,
    output_path: str,
    effects: list[tuple[str, list[str]]],
) -> tuple[bool, str]:
    """Apply pipe effects sequentially.

    Each effect is applied to the output of the previous one.
    Effects: huehsv (ImageMagick), multipitch (SoX), or FFmpeg filters.
    """
    if not effects:
        ok, err = _run_ffmpeg_raw(["ffmpeg", "-y", "-i", input_path, "-c", "copy", output_path], timeout=60)
        return ok, err

    with tempfile.TemporaryDirectory() as tmpdir:
        current = input_path
        ffmpeg_vf_parts = []
        ffmpeg_af_parts = []

        for i, (name, params) in enumerate(effects):
            # ImageMagick effect
            if name == "huehsv":
                val = float(params[0]) if params else 0.5
                out = os.path.join(tmpdir, f"pipe_{i}.mp4")
                ok, err = _run_huehsv(current, out, val)
                if not ok:
                    return False, err
                current = out
                continue

            # SoX multipitch
            if name in ("multipitch", "mp", "multi"):
                out = os.path.join(tmpdir, f"pipe_{i}.mp4")
                ok, err = _run_multipitch(current, out, params)
                if not ok:
                    return False, err
                current = out
                continue

            # TV simulator
            if name in ("tvsim", "tv"):
                line_sync = float(params[0]) if len(params) > 0 else 0.25
                zoom_grill = float(params[1]) if len(params) > 1 else 1.0
                vertical = params[2].lower() in ("1", "true", "t", "y", "yes", "+", "on") if len(params) > 2 else False
                out = os.path.join(tmpdir, f"pipe_{i}.mp4")
                ok, err = _run_tvsim(current, out, line_sync, zoom_grill, vertical)
                if not ok:
                    return False, err
                current = out
                continue

            # FFmpeg video filter
            vf = _build_ffmpeg_pipe_vf(name, params)
            if vf:
                ffmpeg_vf_parts.append(vf)
                continue

            # FFmpeg audio filter
            af = _build_ffmpeg_pipe_vf(name, params)
            if af and name in ("volume", "vibrato", "areverse"):
                ffmpeg_af_parts.append(af)
                continue

            return False, f"Unknown pipe effect: {name}"

        # Apply collected FFmpeg filters in one pass
        if ffmpeg_vf_parts or ffmpeg_af_parts:
            cmd = ["ffmpeg", "-y", "-i", current]
            if ffmpeg_vf_parts:
                cmd.extend(["-vf", ",".join(ffmpeg_vf_parts)])
            if ffmpeg_af_parts:
                cmd.extend(["-af", ",".join(ffmpeg_af_parts)])
            cmd.extend([
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", output_path,
            ])
            ok, err = _run_ffmpeg_raw(cmd, timeout=180)
            if not ok:
                return False, f"FFmpeg pipe filter failed: {err}"
        else:
            # No FFmpeg filters; copy to final output
            if current != output_path:
                shutil.copyfile(current, output_path)

    return True, ""


# ---------- IHTX TagScript workflow ----------

def _parse_ihtx_custom_args(args: str) -> tuple[int, str, str, str, str, str] | None:
    """Parse TagScript-style IHTX custom syntax.

    Syntax:
      <exports> <duration_expr> <no_trim> <export_file_format> <output_file_format> <pipe effects>

    Example:
      10 0.483 - mp4 default huehsv 0.5;negate;multipitch=1|6|7
    """
    parts = shlex.split(args)
    if len(parts) <= 5:
        return None
    try:
        exports = int(parts[0])
    except ValueError:
        return None
    if exports == 0:
        return None
    duration_expr = parts[1]
    no_trim = parts[2]
    export_format = parts[3].lstrip(".") or "mp4"
    output_format = parts[4].lstrip(".") or "default"
    pipe_effects = " ".join(parts[5:]).strip()
    if not pipe_effects:
        return None
    return exports, duration_expr, no_trim, export_format, output_format, pipe_effects


def _safe_awk_duration(duration_expr: str, vidlen: float) -> tuple[bool, str]:
    """Evaluate the tag duration expression using awk like the original TagScript."""
    if not duration_expr or len(duration_expr) > 200:
        return False, "Invalid duration expression."
    if any(ch in duration_expr for ch in "\n\r\0"):
        return False, "Duration expression cannot contain newlines."
    try:
        result = subprocess.run(
            ["awk", "-v", f"vidlen={vidlen}", f"BEGIN{{ printf {duration_expr} }}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as e:
        return False, f"Duration expression failed: {e}"
    if result.returncode != 0:
        return False, result.stderr[-1000:] or "Duration expression failed."
    value = result.stdout.strip()
    try:
        dur = float(value)
    except ValueError:
        return False, f"Duration expression did not produce a number: {value!r}"
    if not math.isfinite(dur) or dur <= 0:
        return False, "Duration must be a positive finite number."
    return True, str(min(dur, MAX_DURATION))


def _concat_codec_args(output_format: str) -> list[str]:
    """Return final concat codec args matching the IHTX TagScript cases."""
    fmt = output_format.lower().lstrip(".")
    if fmt == "mkv":
        return ["-c:v", "mpeg2video", "-q:v", "1", "-c:a", "flac", "-pix_fmt", "yuv420p", "-bufsize", "64M"]
    if fmt == "mxf":
        return ["-c:v", "mpeg2video", "-qscale", "1", "-qmin", "1", "-c:a", "pcm_s16le", "-ar", "48000", "-pix_fmt", "yuv420p", "-bufsize", "64M"]
    if fmt == "mov":
        return ["-c:v", "libx264", "-profile:v", "high422", "-level:v", "5", "-tune", "zerolatency", "-q:v", "1", "-crf", "30", "-preset", "superfast", "-c:a", "aac", "-q:a", "10", "-b:a", "192K", "-aac_coder", "fast", "-pix_fmt", "yuv420p", "-bufsize", "64M"]
    if fmt == "mp4":
        return ["-c:v", "libx264", "-profile:v", "high422", "-level:v", "5", "-tune", "zerolatency", "-q:v", "1", "-crf", "30", "-preset", "superfast", "-c:a", "flac", "-pix_fmt", "yuv420p", "-bufsize", "64M"]
    if fmt == "avi":
        return ["-c:v", "mpeg2video", "-c:a", "flac", "-pix_fmt", "yuv420p"]
    return ["-pix_fmt", "yuv420p", "-bufsize", "64M"]


def _run_ihtx_tagscript_workflow(
    input_path: str,
    output_path: str,
    exports: int,
    duration_expr: str,
    no_trim: str,
    export_format: str,
    output_format: str,
    pipe_effects_str: str,
) -> tuple[bool, str]:
    """Run custom IHTX using the TagScript-style shell workflow with pipe effects.

    Pipe effects are applied sequentially to each export.
    """
    if abs(exports) > MAX_REPETITIONS:
        exports = MAX_REPETITIONS if exports > 0 else -MAX_REPETITIONS

    if not re.fullmatch(r"[A-Za-z0-9]+", export_format):
        return False, "Export file format must be alphanumeric (example: mp4)."
    if output_format != "default" and not re.fullmatch(r"[A-Za-z0-9]+", output_format):
        return False, "Output file format must be alphanumeric or `default`."

    effects = _parse_pipe_effects(pipe_effects_str)
    if not effects:
        return False, "No pipe effects provided."

    vidlen = _ffprobe_duration(input_path)
    if vidlen <= 0:
        return False, "Could not read input duration."
    dur_ok, dur_or_error = _safe_awk_duration(duration_expr, vidlen)
    if not dur_ok:
        return False, dur_or_error
    dur = dur_or_error

    in_ext = Path(input_path).suffix.lower().lstrip(".") or "mp4"
    extension = in_ext if output_format == "default" else output_format.lower().lstrip(".")

    with tempfile.TemporaryDirectory() as tmpdir:
        base = os.path.join(tmpdir, "0.mp4")
        final_output = os.path.join(tmpdir, f"icfplus.{extension}")

        warmup = os.path.join(tmpdir, "a.mp4")
        _run_ffmpeg_raw([
            "ffmpeg", "-loglevel", "error", "-hide_banner", "-y",
            "-i", "https://file.garden/aTXso15ukD3mnuPI/resized.mp4",
            "-vf", "scale=4:4,setsar=1:1,geq=r=128:g=128:b=128",
            "-pix_fmt", "yuv420p", "-preset", "ultrafast", "-an", "-t", "0.03", warmup,
        ], timeout=60)

        ok, err = _run_ffmpeg_raw([
            "ffmpeg", "-loglevel", "error", "-hide_banner", "-y",
            "-stream_loop", "-1", "-i", input_path,
            "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "16M",
            "-c:a", "flac", "-t", dur, "-movflags", "+faststart", base,
        ], timeout=180)
        if not ok:
            return False, f"Base render failed: {err}"

        no_trim_enabled = no_trim.lower() in {"true", "yes"}
        total_exports = abs(exports)
        previous = base
        for i in range(1, total_exports + 2):
            current = os.path.join(tmpdir, f"{i}.{export_format}")
            ok, err = _apply_pipe_effects(previous, current, effects)
            if not ok:
                return False, f"Export {i} failed: {err}"
            previous = current

        concat_list = os.path.join(tmpdir, "concat.txt")
        sequence = range(total_exports, 0, -1) if exports < 0 else range(1, total_exports + 1)
        with open(concat_list, "w") as f:
            for i in sequence:
                f.write(f"file '{os.path.join(tmpdir, f'{i}.{export_format}')}'\n")

        concat_cmd = [
            "ffmpeg", "-loglevel", "error", "-hide_banner", "-y",
            "-f", "concat", "-safe", "0", "-i", concat_list,
        ]
        concat_cmd.extend(_concat_codec_args(extension))
        concat_cmd.extend(["-movflags", "+faststart", final_output])
        ok, err = _run_ffmpeg_raw(concat_cmd, timeout=300)
        if not ok:
            return False, f"Concat failed: {err}"
        shutil.copyfile(final_output, output_path)

    return True, ""


# ---------- Multipitch (SoX pitch-shift pipeline) ----------

def _run_multipitch(
    input_path: str,
    output_path: str,
    pitch_values: list[str],
) -> tuple[bool, str]:
    """Run the multipitch pipeline using SoX pitch shifting + ffmpeg re-merge.

    Pipeline per pitch voice:
      1. ffmpeg -i input -c:a pcm_s16le -preset ultrafast a0.mp4  (export segment)
      2. ffmpeg -i a0.mp4 b0.wav                                   (extract WAV)
      3. sox b0.wav 1{i}.wav pitch -q {semitones*100} 25 5 8.5     (SoX pitch shift)
      4. Single pitch: ffmpeg -i a0.mp4 -i 10.wav -filter_complex "[1:a]highpass=10[a]"
         -map 0:v -map "[a]" -c:a pcm_s16le output
         Multiple pitches: ffmpeg -i a0.mp4 -i 10.wav -i 11.wav ...
         -filter_complex "[1][2]...amix=N:normalize=0,highpass=7.5"
         -c:a pcm_s16le -preset ultrafast output

    SoX pitch effect syntax: pitch -q cents 25 5 8.5
      where cents = semitones * 100
    """
    # Flatten pitch values: support both | and ; separators
    flattened = []
    for pv in pitch_values:
        if "|" in pv:
            flattened.extend([v.strip() for v in pv.split("|") if v.strip()])
        elif ";" in pv:
            flattened.extend([v.strip() for v in pv.split(";") if v.strip()])
        else:
            flattened.append(pv.strip())
    pitch_values = flattened

    if not pitch_values:
        return False, "No pitch values provided."

    n = len(pitch_values)

    # Validate pitch values
    for pv in pitch_values:
        try:
            float(pv)
        except ValueError:
            return False, f"Invalid pitch value: {pv!r}"

    with tempfile.TemporaryDirectory() as tmpdir:
        # Step 1: Export base video with pcm_s16le audio
        base_mp4 = os.path.join(tmpdir, "a0.mp4")
        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-c:a", "pcm_s16le", "-preset", "ultrafast",
            base_mp4,
        ]
        ok, err = _run_ffmpeg_raw(cmd, timeout=120)
        if not ok:
            return False, f"Step 1 (export base) failed: {err}"

        # Step 2: Extract WAV audio from base
        base_wav = os.path.join(tmpdir, "b0.wav")
        cmd = [
            "ffmpeg", "-y", "-i", base_mp4,
            base_wav,
        ]
        ok, err = _run_ffmpeg_raw(cmd, timeout=120)
        if not ok:
            return False, f"Step 2 (extract WAV) failed: {err}"

        # Step 3: SoX pitch shift for each voice
        pitched_wavs = []
        for i, pitch_val in enumerate(pitch_values):
            cents = int(float(pitch_val) * 100)
            out_wav = os.path.join(tmpdir, f"1{i}.wav")
            cmd = [
                "sox", base_wav, out_wav,
                "pitch", "-q", str(cents), "25", "5", "8.5",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                return False, f"SoX pitch shift for voice {i+1} failed: {result.stderr}"
            pitched_wavs.append(out_wav)

        # Step 4: Re-merge with video
        if n == 1:
            # Single pitch: ffmpeg -i a0.mp4 -i 10.wav -filter_complex "[1:a]highpass=10[a]"
            #   -map 0:v -map "[a]" -c:a pcm_s16le output
            cmd = [
                "ffmpeg", "-y",
                "-i", base_mp4,
                "-i", pitched_wavs[0],
                "-filter_complex", "[1:a]highpass=10[a]",
                "-map", "0:v", "-map", "[a]",
                "-c:a", "pcm_s16le",
                output_path,
            ]
        else:
            # Multiple pitches: ffmpeg -i a0.mp4 -i 10.wav -i 11.wav ...
            #   -filter_complex "[1][2]...[N]amix=N:normalize=0,highpass=7.5"
            #   -c:a pcm_s16le -preset ultrafast output
            cmd = ["ffmpeg", "-y", "-i", base_mp4]
            for wav in pitched_wavs:
                cmd.extend(["-i", wav])

            # Build filter_complex: [1][2]...[N]amix=N:normalize=0,highpass=7.5
            mix_inputs = "".join(f"[{i+1}]" for i in range(n))
            filter_complex = f"{mix_inputs}amix={n}:normalize=0,highpass=7.5"

            cmd.extend([
                "-filter_complex", filter_complex,
                "-map", "0:v",
                "-c:a", "pcm_s16le", "-preset", "ultrafast",
                output_path,
            ])

        return _run_ffmpeg_raw(cmd, timeout=300)





def _build_atempo_chain(speed: float) -> str:
    """Build an atempo filter chain that handles FFmpeg's 0.5–100.0 bounds.

    FFmpeg's atempo filter only accepts values between 0.5 and 100.0.
    For speeds outside this range, chain multiple atempo filters.
    """
    if 0.5 <= speed <= 100.0:
        return f"atempo={speed}"
    # Chain multiple atempo filters
    parts = []
    remaining = speed
    while remaining > 100.0:
        parts.append("atempo=100.0")
        remaining /= 100.0
    while remaining < 0.5:
        parts.append("atempo=0.5")
        remaining /= 0.5
    parts.append(f"atempo={remaining}")
    return ",".join(parts)


# ---------- Syncaudio (video/audio duration sync) ----------

def _run_syncaudio(
    input_path: str,
    output_path: str,
    alt_mode: bool = False,
) -> tuple[bool, str]:
    """Sync video and audio durations by adjusting playback speed.

    Default mode: adjust video speed (setpts) to match audio duration.
    Alt mode: adjust audio speed (atempo) to match video duration.

    Returns (ok, info_string_or_error).
    """
    # Extract video-only and audio-only streams
    with tempfile.TemporaryDirectory() as tmpdir:
        v_only = os.path.join(tmpdir, "v.mp4")
        a_only = os.path.join(tmpdir, "a.wav")

        # Extract video without audio
        cmd = ["ffmpeg", "-y", "-i", input_path, "-an", v_only]
        ok, err = _run_ffmpeg_raw(cmd, timeout=120)
        if not ok:
            return False, f"Failed to extract video stream: {err}"

        # Extract audio as WAV
        cmd = ["ffmpeg", "-y", "-i", input_path, a_only]
        ok, err = _run_ffmpeg_raw(cmd, timeout=120)
        if not ok:
            return False, f"Failed to extract audio stream: {err}"

        # Get durations
        vd = _ffprobe_duration(v_only)
        ad = _ffprobe_duration(a_only)

        # Get frame rate
        fr_out = _ffprobe(v_only, "-select_streams", "v:0",
                          "-show_entries", "stream=r_frame_rate",
                          "-of", "default=nokey=1:noprint_wrappers=1")

        if vd <= 0 or ad <= 0:
            return False, f"Could not determine durations (video={vd:.3f}s, audio={ad:.3f}s)"

        if alt_mode:
            # Alt mode: adjust audio speed (atempo) to match video duration
            speed = ad / vd
            # FFmpeg atempo supports 0.5–100.0; chain if needed
            atempo_filter = _build_atempo_chain(speed)
            cmd = [
                "ffmpeg", "-y",
                "-i", v_only,
                "-stream_loop", "-1", "-i", a_only,
                "-af", atempo_filter,
                "-map", "0:v", "-map", "1:a",
                "-t", str(vd),
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                output_path,
            ]
        else:
            # Default mode: adjust video speed (setpts) to match audio duration
            speed = vd / ad
            # Use setpts to change video speed, keep original frame rate
            fps_filter = f"setpts=1/({speed})*PTS"
            if fr_out:
                fps_filter += f",fps={fr_out}"
            cmd = [
                "ffmpeg", "-y",
                "-i", v_only,
                "-stream_loop", "-1", "-i", a_only,
                "-vf", fps_filter,
                "-map", "0:v", "-map", "1:a",
                "-t", str(max(vd, ad)),
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                output_path,
            ]

        ok, err = _run_ffmpeg_raw(cmd, timeout=300)
        if not ok:
            return False, f"Sync failed: {err}"

        diff = vd - ad
        info = (
            f"Video: {vd:.3f}s\n"
            f"Audio: {ad:.3f}s\n\n"
            f"Speed Used: {speed:.6f}\n"
            f"Diff: {diff:.6f}"
        )
        return True, info

# ---------- Preview1280 (TV-simulator montage) ----------

async def _ensure_displacement_map(workdir: str) -> str:
    """Ensure the TV simulator displacement map exists, downloading if needed.

    Returns the path to the .mov file.
    """
    # First check if the bundled copy exists
    bundled = Path("bot/displacemaps/tvsimulator.mov")
    if bundled.exists():
        return str(bundled)

    # Try to download it
    disp_dir = os.path.join(workdir, "displacemaps")
    os.makedirs(disp_dir, exist_ok=True)
    dest = os.path.join(disp_dir, "tvsimulator.mov")
    if os.path.exists(dest):
        return dest

    try:
        await download_url(
            "https://file.garden/aTXso15ukD3mnuPI/tv_sim_displacement_map.mov",
            dest
        )
        return dest
    except Exception:
        # Last resort: check common locations
        for candidate in [
            "displacemaps/tvsimulator.mov",
            "bot/displacemaps/tvsimulator.mov",
            "/app/bot/displacemaps/tvsimulator.mov",
        ]:
            if os.path.exists(candidate):
                return candidate
        raise FileNotFoundError(
            "TV simulator displacement map not found and could not be downloaded. "
            "Place it at bot/displacemaps/tvsimulator.mov"
        )


def _generate_hald_cluts(workdir: str) -> list[str]:
    """Generate Hald CLUT .ppm files for hue shifts using ImageMagick.

    Returns paths to [hslhue_54.ppm, hslhue_180.ppm, hslhue_22.ppm, hslhue_108_30.ppm].
    CLUT hue values use ImageMagick -modulate formula: hue_frac * 200 + 100 (or +200 for sat boost).
    """
    # (filename, brightness, saturation, hue_mod_value)
    # hue_mod_value = hue_fraction * 200 + 100
    # For saturation-boosted CLUTs, saturation > 100 and hue_mod = hue_fraction * 200 + 200
    clut_specs = [
        # hslhue_54: hue shift 54° → fraction 0.15, mod = 0.15*200+100 = 130
        ("hslhue_54.ppm", 100, 100, 130),
        # hslhue_180: hue shift 180° → fraction 0.5, mod = 0.5*200+100 = 200
        ("hslhue_180.ppm", 100, 100, 200),
        # hslhue_22: hue shift 22° → fraction 0.06, mod = 0.06*200+100 = 112
        ("hslhue_22.ppm", 100, 100, 112),
        # hslhue_108_30: hue shift 108° + saturation boost → fraction 0.3, mod = 0.3*200+200 = 260
        ("hslhue_108_30.ppm", 100, 130, 260),
    ]
    paths = []
    for i, (filename, brightness, saturation, hue_mod) in enumerate(clut_specs):
        path = os.path.join(workdir, filename)
        cmd = [
            "magick", "hald:4",
            "-modulate", f"{brightness},{saturation},{hue_mod}",
            path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            # Fallback: create a simple identity CLUT
            # If magick isn't available, skip CLUT effects
            pass
        # For hslhue_108_30 (index 3), apply additional -modulate 100,100,0
        if i == 3 and os.path.exists(path):
            extra_cmd = [
                "magick", path,
                "-modulate", "100,100,0",
                path
            ]
            subprocess.run(extra_cmd, capture_output=True, text=True, timeout=30)
        paths.append(path)
    return paths


def _run_preview1280(
    input_path: str,
    output_path: str,
    start_offset: float = 1.85,
    segment_dur: float = 0.85,
) -> tuple[bool, str]:
    """Run the preview1280 TV-simulator montage pipeline.

    This creates a 12-segment montage at 640x360, then scales to original size.
    Requires: ffmpeg, ImageMagick (magick), and the tvsimulator.mov displacement map.
    Uses rubberband audio filter for high-quality pitch shifting.
    """
    # Helper: rubberband pitch filter string for N semitones
    # Pre-compute 2^(N/12) as a float to avoid FFmpeg expression parsing issues
    def _rb(semitones: float, transients: str = "mixed") -> str:
        pitch_ratio = 2 ** (semitones / 12)
        return (
            f"rubberband=pitch={pitch_ratio:.6f}:"
            f"window=short:transients={transients}:"
            f"detector=soft:channels=together:pitchq=consistency"
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        info = _ffprobe_video_info(input_path)
        w, h = info["width"], info["height"]
        dur = info["duration"]

        if w == 0 or h == 0:
            return False, "Could not read input video dimensions."

        # Generate Hald CLUTs
        cluts = _generate_hald_cluts(tmpdir)
        clut_54 = cluts[0] if os.path.exists(cluts[0]) else None
        clut_180 = cluts[1] if os.path.exists(cluts[1]) else None
        clut_22 = cluts[2] if os.path.exists(cluts[2]) else None
        clut_108_30 = cluts[3] if os.path.exists(cluts[3]) else None

        # Locate displacement map
        disp_map = None
        for candidate in [
            "bot/displacemaps/tvsimulator.mov",
            "displacemaps/tvsimulator.mov",
            "/app/bot/displacemaps/tvsimulator.mov",
        ]:
            if os.path.exists(candidate):
                disp_map = candidate
                break

        # Compute timing
        t = segment_dur
        t2 = segment_dur / 2
        t3 = start_offset + segment_dur

        # Step 1: Pre-process input to 640x360 FFV1
        avi0 = os.path.join(tmpdir, "0.avi")
        cmd = [
            "ffmpeg", "-y", "-stream_loop", "-1", "-i", input_path,
            "-vf", "scale=640:360,setsar=1:1",
            "-ss", str(start_offset), "-to", str(t3),
            "-c:v", "ffv1", "-c:a", "pcm_s16le",
            avi0
        ]
        ok, err = _run_ffmpeg_raw(cmd, timeout=120)
        if not ok:
            return False, f"Step 1 (pre-process) failed: {err}"

        avi_w = _ffprobe(avi0, "-select_streams", "v:0",
                         "-show_entries", "stream=width",
                         "-of", "default=nw=1:nk=1") or "640"
        avi_h = _ffprobe(avi0, "-select_streams", "v:0",
                         "-show_entries", "stream=height",
                         "-of", "default=nw=1:nk=1") or "360"

        # Helper to build segment ffmpeg commands
        segments = []

        # Segment 1: plain copy, duration t
        seg1 = os.path.join(tmpdir, "1.avi")
        segments.append(([
            "ffmpeg", "-y", "-i", avi0,
            "-t", str(t), "-c:v", "ffv1", "-c:a", "pcm_s16le",
            seg1
        ], seg1))

        # Segment 2: hue +54 (hslhue_54), pitch +1 semitone (rubberband)
        seg2 = os.path.join(tmpdir, "2.avi")
        if clut_54:
            segments.append(([
                "ffmpeg", "-y", "-i", avi0,
                "-vf", f"movie={clut_54},[in]haldclut,format=yuv420p",
                "-af", _rb(1),
                "-t", str(t), "-c:v", "ffv1", "-c:a", "pcm_s16le",
                seg2
            ], seg2))
        else:
            segments.append(([
                "ffmpeg", "-y", "-i", avi0,
                "-vf", "hue=h=54",
                "-af", _rb(1),
                "-t", str(t), "-c:v", "ffv1", "-c:a", "pcm_s16le",
                seg2
            ], seg2))

        # Segment 3: hue +180 + displacement map + mirror + pitch -2 semitones
        seg3 = os.path.join(tmpdir, "3.avi")
        if disp_map and clut_180:
            fc = (
                f"movie={clut_180}[h];"
                f"[0][h]haldclut,hflip,crop=iw/2:ih:0:0,split[left][tmp];"
                f"[tmp]hflip[right];[left][right]hstack,format=yuv420p,format=bgr32[00];"
                f"[1]crop=iw:ih/1:0:0,scale={avi_w}:{avi_h},eq=contrast=0.4,format=bgr32,hue=b=-0.033[x];"
                f"nullsrc=1x1,geq=r=128:g=128:b=128,scale={avi_w}:{avi_h},format=bgr32[y];"
                f"[00][x][y]displace=edge=wrap[v]"
            )
            segments.append(([
                "ffmpeg", "-y", "-i", avi0, "-stream_loop", "-1", "-i", disp_map,
                "-filter_complex", fc,
                "-af", _rb(-2),
                "-map", "[v]", "-map", "0:a",
                "-pix_fmt", "yuv420p",
                "-t", str(t), "-c:v", "ffv1", "-c:a", "pcm_s16le",
                seg3
            ], seg3))
        else:
            # Fallback without displacement
            segments.append(([
                "ffmpeg", "-y", "-i", avi0,
                "-vf", "hue=h=180,hflip,crop=iw/2:ih:0:0,split[left][tmp];[tmp]hflip[right];[left][right]hstack,format=yuv420p",
                "-af", _rb(-2),
                "-t", str(t), "-c:v", "ffv1", "-c:a", "pcm_s16le",
                seg3
            ], seg3))

        # Segment 4: hue +54 (hslhue_54), pitch +1 semitone (same as seg2)
        seg4 = os.path.join(tmpdir, "4.avi")
        if clut_54:
            segments.append(([
                "ffmpeg", "-y", "-i", avi0,
                "-vf", f"movie={clut_54},[in]haldclut,format=yuv420p",
                "-af", _rb(1),
                "-t", str(t), "-c:v", "ffv1", "-c:a", "pcm_s16le",
                seg4
            ], seg4))
        else:
            segments.append(([
                "ffmpeg", "-y", "-i", avi0,
                "-vf", "hue=h=54",
                "-af", _rb(1),
                "-t", str(t), "-c:v", "ffv1", "-c:a", "pcm_s16le",
                seg4
            ], seg4))

        # Segments 5-12: shorter segments (t2 duration)
        short_specs = [
            # (seg_num, vf_filter, af_filter)
            (5, None, None),  # plain copy
            (6, f"movie={clut_22},[in]haldclut,hflip,format=yuv420p" if clut_22 else "hue=h=22,hflip,format=yuv420p",
             _rb(2, "smooth")),  # hue+22, hflip, pitch+2 (smooth transients)
            (7, f"movie={clut_54},[in]haldclut,format=yuv420p" if clut_54 else "hue=h=54,format=yuv420p",
             _rb(1)),  # hue+54, pitch+1
            (8, f"movie={clut_108_30},[in]haldclut,hflip,format=yuv420p" if clut_108_30 else "hue=h=108,hflip,format=yuv420p",
             _rb(3)),  # hue+108+sat30, hflip, pitch+3
            (9, f"movie={clut_180},[in]haldclut,format=yuv420p" if clut_180 else "hue=h=180,format=yuv420p",
             _rb(-2)),  # hue+180, pitch-2
            (10, "hflip", None),  # just hflip
            (11, f"movie={clut_54},[in]haldclut,format=yuv420p" if clut_54 else "hue=h=54,format=yuv420p",
             _rb(1)),  # hue+54, pitch+1
            (12, f"movie={clut_108_30},[in]haldclut,hflip,format=yuv420p" if clut_108_30 else "hue=h=108,hflip,format=yuv420p",
             _rb(3)),  # hue+108+sat30, hflip, pitch+3
        ]

        for seg_num, vf, af in short_specs:
            seg_path = os.path.join(tmpdir, f"{seg_num}.avi")
            cmd = ["ffmpeg", "-y", "-i", avi0]
            if vf:
                cmd.extend(["-vf", vf])
            if af:
                cmd.extend(["-af", af])
            cmd.extend(["-t", str(t2), "-c:v", "ffv1", "-c:a", "pcm_s16le", seg_path])
            segments.append((cmd, seg_path))

        # Render all segments
        for i, (cmd, seg_path) in enumerate(segments):
            ok, err = _run_ffmpeg_raw(cmd, timeout=120)
            if not ok:
                return False, f"Segment {i+1}/{len(segments)} failed: {err}"

        # Concat all segments using concat protocol
        avi_files = [sp for _, sp in segments if os.path.exists(sp)]
        if not avi_files:
            return False, "No segments were produced."

        concat_str = "|".join(avi_files)
        concat_out = os.path.join(tmpdir, "concat.avi")
        cmd = [
            "ffmpeg", "-y",
            "-i", f"concat:{concat_str}",
            "-vf", f"scale={w}:{h},setsar=1",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            output_path
        ]
        return _run_ffmpeg_raw(cmd, timeout=180)


# ---------- Bot events & commands ----------

@bot.event
async def on_ready():
    print(f"IHTX Bot online as {bot.user} (ID: {bot.user.id})")
    print("------")
    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except discord.HTTPException as e:
        if "50240" in str(e):
            print("Entry Point command conflict — skipping bulk sync (slash commands already registered)")
        else:
            print(f"Failed to sync slash commands: {e}")
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching,
        name="Meet the Sparkles! ✨👗 | Sparkles Magical Market Full Episode | Cartoons for Kids"
    ))


@bot.hybrid_command(name="ihtx", aliases=["effect", "destroy"], description="HEAVY COMMAND: replicates ihtx from FFmpeg")
@app_commands.describe(args="Preset name or effect chain (e.g. chaos, huehsv 0.5;negate;multipitch=1|6|7)", attachment="Video or image file to process")
async def ihtx_command(ctx: commands.Context, *, args: str = "chaos", attachment: discord.Attachment = None):
    """HEAVY COMMAND: replicates ihtx from FFmpeg.

    Apply an IHTX FFmpeg effect to an attached video or image.

    Usage:
      g!ihtx [preset]                  — use a built-in preset (chaos, glitch, etc.)
      g!ihtx <exports> <duration> <no_trim> <export_fmt> <output_fmt> <pipe effects>   — custom TagScript workflow
    """
    # Parse arguments: preset name or TagScript-style custom icf+ workflow.
    parts = args.split()
    first = parts[0].lower() if parts else "chaos"

    is_preset = first in VISUAL_PRESETS and len(parts) == 1
    custom_args = None if is_preset else _parse_ihtx_custom_args(args)

    if is_preset:
        preset = first
    elif custom_args is None:
        preset_list = ", ".join(f"`{p}`" for p in sorted(VISUAL_PRESETS))
        await ctx.reply(
            f"Unknown preset or invalid custom IHTX syntax. Available presets: {preset_list}\n"
            f"Custom syntax: `g!ihtx <exports> <duration> <no_trim> <export_fmt> <output_fmt> <pipe effects>`\n"
            f"Example: `g!ihtx 10 0.483 - mp4 default huehsv 0.5;negate;multipitch=1|6|7`\n"
            f"Use `g!ihtxhelp` for full usage."
        )
        return

    # Resolve attachment: slash commands pass it as a parameter;
    # prefix commands need us to look at the message or referenced message.
    if attachment is None:
        if ctx.message and ctx.message.attachments:
            attachment = ctx.message.attachments[0]
        elif ctx.message and ctx.message.reference:
            try:
                ref = await ctx.channel.fetch_message(ctx.message.reference.message_id)
                if ref.attachments:
                    attachment = ref.attachments[0]
            except Exception:
                pass

    if not attachment:
        preset_list = ", ".join(f"`{p}`" for p in sorted(VISUAL_PRESETS))
        await ctx.reply(
            f"**I HATE THE X — IHTX Bot**\n"
            f"Attach a video or image and use `g!ihtx [preset]` or the custom IHTX syntax.\n\n"
            f"**Presets:** {preset_list}\n\n"
            f"**Custom IHTX:** `g!ihtx 10 0.483 - mp4 default huehsv 0.5;negate;multipitch=1|6|7`\n"
            f"Use `g!ihtxhelp` for full usage.\n\n"
            f"Examples:\n"
            f"`g!ihtx chaos`\n"
            f"`g!ihtx glitch`\n"
            f"`g!ihtx 10 0.5 - mp4 default huehsv 0.5;negate;multipitch=25|5|8.5`\n"
            f"`g!ihtx 5 0.25 - mp4 default multipitch=1|2|3|4`"
        )
        return

    if attachment.size > MAX_FILE_SIZE:
        await ctx.reply(f"File too large (max 25 MB). Your file is {attachment.size / 1024 / 1024:.1f} MB.")
        return

    suffix = Path(attachment.filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        await ctx.reply(f"Unsupported file type `{suffix}`. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
        return

    is_video = suffix in VIDEO_EXTENSIONS
    out_ext = get_output_ext(suffix, is_video)

    status_msg = await ctx.reply(
        f"⚙️ Applying **{'preset: ' + preset if is_preset else 'custom IHTX TagScript workflow'}**... this may take a moment."
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, f"input{suffix}")
        output_path = os.path.join(tmpdir, f"output{out_ext}")

        try:
            await download_attachment(attachment, input_path)
        except Exception as e:
            await status_msg.edit(content=f"❌ Failed to download your file: {e}")
            return

        loop = asyncio.get_event_loop()

        if is_preset:
            ok, err = await loop.run_in_executor(
                None, run_ffmpeg, input_path, output_path, preset, is_video
            )
        else:
            # Custom IHTX follows the TagScript icf+ shell workflow and only supports video.
            if not is_video:
                await status_msg.edit(content="❌ Custom IHTX workflow requires video input (not images/GIFs).")
                return
            exports, duration_expr, no_trim, export_format, output_format, pipe_effects = custom_args
            output_ext = suffix if output_format == "default" else f".{output_format.lstrip('.')}"
            output_path = os.path.join(tmpdir, f"output{output_ext}")
            ok, err = await loop.run_in_executor(
                None, _run_ihtx_tagscript_workflow,
                input_path, output_path, exports, duration_expr, no_trim,
                export_format, output_format, pipe_effects
            )

        if not ok:
            await status_msg.edit(content=f"❌ FFmpeg failed:\n```\n{err[-1500:]}\n```")
            return

        out_size = os.path.getsize(output_path)
        if out_size > MAX_FILE_SIZE:
            await status_msg.edit(content="❌ Output file too large for Discord (>25 MB). Try a shorter clip.")
            return

        if is_preset:
            out_filename = f"ihtx_{preset}_{Path(attachment.filename).stem}{out_ext}"
        else:
            out_filename = f"ihtx_custom_{Path(attachment.filename).stem}{out_ext}"

        try:
            await ctx.reply(
                content=f"✅ **IHTX `{'preset: ' + preset if is_preset else 'custom'}`** applied!\n⚠️ Make sure you use `g!syncaudio` or `g!syncaudio alt` afterwards to make sure the video is synced to the audio.",
                file=discord.File(output_path, filename=out_filename),
            )
            await status_msg.delete()
        except discord.HTTPException as e:
            await status_msg.edit(content=f"❌ Failed to upload result: {e}")


@bot.hybrid_command(name="preview1280", aliases=["p1280"], description="Create a 12-segment TV-simulator preview montage")
@app_commands.describe(start="Start offset in seconds (default: 1.85)", duration="Segment duration in seconds (default: 0.85)", attachment="Video file to preview")
async def preview1280_command(ctx: commands.Context, start: float = 1.85, duration: float = 0.85, attachment: discord.Attachment = None):
    """Create a 12-segment TV-simulator preview montage from an attached video.

    Usage: g!preview1280 [start_offset] [segment_duration]
    Default: start=1.85, duration=0.85
    """
    # Resolve attachment: slash commands pass it as a parameter;
    # prefix commands need us to look at the message or referenced message.
    if attachment is None:
        if ctx.message and ctx.message.attachments:
            attachment = ctx.message.attachments[0]
        elif ctx.message and ctx.message.reference:
            try:
                ref = await ctx.channel.fetch_message(ctx.message.reference.message_id)
                if ref.attachments:
                    attachment = ref.attachments[0]
            except Exception:
                pass

    if not attachment:
        await ctx.reply(
            "**IHTX Preview1280**\n"
            "Attach a video and use `g!preview1280 [start] [duration]`.\n\n"
            "Creates a 12-segment TV-simulator montage with hue shifts, "
            "displacement mapping, and pitch variations.\n\n"
            "Defaults: start=1.85s, duration=0.85s per segment.\n"
            "Example: `g!preview1280 2.0 1.0`"
        )
        return

    if attachment.size > MAX_FILE_SIZE:
        await ctx.reply(f"File too large (max 25 MB). Your file is {attachment.size / 1024 / 1024:.1f} MB.")
        return

    suffix = Path(attachment.filename).suffix.lower()
    if suffix not in VIDEO_EXTENSIONS:
        await ctx.reply(f"Preview1280 requires a video file. Got `{suffix}`.")
        return

    start = max(0.0, start)
    duration = max(0.1, min(duration, 10.0))

    status_msg = await ctx.reply(
        f"⚙️ Creating **preview1280** montage (start={start}s, dur={duration}s)... this will take a while."
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, f"input{suffix}")
        output_path = os.path.join(tmpdir, "output_p1280.mp4")

        try:
            await download_attachment(attachment, input_path)
        except Exception as e:
            await status_msg.edit(content=f"❌ Failed to download your file: {e}")
            return

        # Ensure displacement map is available
        try:
            disp_path = await _ensure_displacement_map(tmpdir)
        except FileNotFoundError as e:
            await status_msg.edit(content=f"❌ {e}")
            return

        loop = asyncio.get_event_loop()
        ok, err = await loop.run_in_executor(
            None, _run_preview1280, input_path, output_path, start, duration
        )

        if not ok:
            await status_msg.edit(content=f"❌ Preview1280 failed:\n```\n{err[-1500:]}\n```")
            return

        out_size = os.path.getsize(output_path)
        if out_size > MAX_FILE_SIZE:
            await status_msg.edit(content="❌ Output file too large for Discord (>25 MB). Try shorter segments.")
            return

        out_filename = f"p1280_{Path(attachment.filename).stem}.mp4"
        try:
            await ctx.reply(
                content=f"✅ **IHTX preview1280** applied!",
                file=discord.File(output_path, filename=out_filename),
            )
            await status_msg.delete()
        except discord.HTTPException as e:
            await status_msg.edit(content=f"❌ Failed to upload result: {e}")




@bot.hybrid_command(name="multipitch", aliases=["mp", "multi"], description="Multi-voice pitch shift via SoX")
@app_commands.describe(args="Pipe-separated semitone values (e.g. 25|5|8.5)", attachment="Video or audio file to pitch-shift")
async def multipitch_command(ctx: commands.Context, *, args: str = "", attachment: discord.Attachment = None):
    """Apply multi-voice pitch shifting to an attached video using SoX pitch.

    Usage:
      g!multipitch <pitch_values>     — pipe-separated semitone values
      g!mp 25|5|8.5                    — aliases
      g!multi -3|0|5                  — negative values supported

    Example: g!multipitch 25|5|8.5
    """
    if not args:
        await ctx.reply(
            "**IHTX Multipitch**\n"
            "Attach a video and use `g!multipitch <pitches>`.\n\n"
            "Pitches are pipe-separated semitone values.\n"
            "Each pitch creates a separate shifted voice, then they are mixed together.\n\n"
            "Example: `g!multipitch 25|5|8.5`\n"
            "Aliases: `g!mp`, `g!multi`"
        )
        return

    # Parse pipe-separated pitch values
    pitch_values = [v.strip() for v in args.split("|") if v.strip()]
    if not pitch_values:
        await ctx.reply("No pitch values provided. Use pipe-separated values like `25|5|8.5`.")
        return

    # Resolve attachment: slash commands pass it as a parameter;
    # prefix commands need us to look at the message or referenced message.
    if attachment is None:
        if ctx.message and ctx.message.attachments:
            attachment = ctx.message.attachments[0]
        elif ctx.message and ctx.message.reference:
            try:
                ref = await ctx.channel.fetch_message(ctx.message.reference.message_id)
                if ref.attachments:
                    attachment = ref.attachments[0]
            except Exception:
                pass

    if not attachment:
        await ctx.reply(
            "Attach a video and use `g!multipitch <pitches>`.\n"
            "Example: `g!multipitch 25|5|8.5`"
        )
        return

    if attachment.size > MAX_FILE_SIZE:
        await ctx.reply(f"File too large (max 25 MB). Your file is {attachment.size / 1024 / 1024:.1f} MB.")
        return

    suffix = Path(attachment.filename).suffix.lower()
    if suffix not in VIDEO_EXTENSIONS:
        await ctx.reply(f"Multipitch requires a video file. Got `{suffix}`.")
        return

    pitch_str = "|".join(pitch_values)
    status_msg = await ctx.reply(
        f"⚙️ Applying **multipitch** ({pitch_str})... this may take a moment."
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, f"input{suffix}")
        output_path = os.path.join(tmpdir, "output_multipitch.mp4")

        try:
            await download_attachment(attachment, input_path)
        except Exception as e:
            await status_msg.edit(content=f"❌ Failed to download your file: {e}")
            return

        loop = asyncio.get_event_loop()
        ok, err = await loop.run_in_executor(
            None, _run_multipitch,
            input_path, output_path, pitch_values
        )

        if not ok:
            await status_msg.edit(content=f"❌ Multipitch failed:\n```\n{err[-1500:]}\n```")
            return

        out_size = os.path.getsize(output_path)
        if out_size > MAX_FILE_SIZE:
            await status_msg.edit(content="❌ Output file too large for Discord (>25 MB). Try a shorter clip.")
            return

        out_filename = f"multipitch_{pitch_str}_{Path(attachment.filename).stem}.mp4"
        try:
            await ctx.reply(
                content=f"✅ **IHTX multipitch** ({pitch_str}) applied!",
                file=discord.File(output_path, filename=out_filename),
            )
            await status_msg.delete()
        except discord.HTTPException as e:
            await status_msg.edit(content=f"❌ Failed to upload result: {e}")




@bot.hybrid_command(name="huehsv", aliases=["hhsv"], description="Apply hue shift via ImageMagick haldclut")
@app_commands.describe(hue="Hue value (e.g. 0.5)", attachment="Video or image to hue-shift")
async def huehsv_command(ctx: commands.Context, hue: float = 0.5, attachment: discord.Attachment = None):
    """Apply hue shift using ImageMagick haldclut + FFmpeg.

    Usage:
      g!huehsv <hue>          — shift hue, default 0.5
      g!hhsv <hue>            — alias

    Internally: magick hald:6 -modulate 100,100,<hue*200+100> hsv.ppm
    Then: ffmpeg -vf "movie=hsv.ppm,[in]haldclut,format=rgba" -pix_fmt yuv420p
    """
    # Resolve attachment
    if attachment is None:
        if ctx.message and ctx.message.attachments:
            attachment = ctx.message.attachments[0]
        elif ctx.message and ctx.message.reference:
            try:
                ref = await ctx.channel.fetch_message(ctx.message.reference.message_id)
                if ref.attachments:
                    attachment = ref.attachments[0]
            except Exception:
                pass

    if not attachment:
        await ctx.reply(
            "**IHTX HueHSV**\n"
            "Attach a video or image and use `g!huehsv <hue>`.\n\n"
            "Applies hue shift via ImageMagick haldclut.\n"
            "Example: `g!huehsv 0.5`\n"
            "Aliases: `g!hhsv`"
        )
        return

    if attachment.size > MAX_FILE_SIZE:
        await ctx.reply(f"File too large (max 25 MB). Your file is {attachment.size / 1024 / 1024:.1f} MB.")
        return

    suffix = Path(attachment.filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        await ctx.reply(f"Unsupported file type `{suffix}`. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
        return

    is_video = suffix in VIDEO_EXTENSIONS
    out_ext = get_output_ext(suffix, is_video)

    status_msg = await ctx.reply(
        f"⚙️ Applying **huehsv** (hue={hue})... this may take a moment."
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, f"input{suffix}")
        output_path = os.path.join(tmpdir, f"output{out_ext}")

        try:
            await download_attachment(attachment, input_path)
        except Exception as e:
            await status_msg.edit(content=f"❌ Failed to download your file: {e}")
            return

        loop = asyncio.get_event_loop()
        ok, err = await loop.run_in_executor(
            None, _run_huehsv, input_path, output_path, hue
        )

        if not ok:
            await status_msg.edit(content=f"❌ HueHSV failed:\n```\n{err[-1500:]}\n```")
            return

        out_size = os.path.getsize(output_path)
        if out_size > MAX_FILE_SIZE:
            await status_msg.edit(content="❌ Output file too large for Discord (>25 MB). Try a shorter clip.")
            return

        out_filename = f"huehsv_{hue}_{Path(attachment.filename).stem}{out_ext}"
        try:
            await ctx.reply(
                content=f"✅ **IHTX huehsv** (hue={hue}) applied!",
                file=discord.File(output_path, filename=out_filename),
            )
            await status_msg.delete()
        except discord.HTTPException as e:
            await status_msg.edit(content=f"❌ Failed to upload result: {e}")


@bot.hybrid_command(name="syncaudio", aliases=["sa", "sync"], description="Sync video and audio durations by adjusting playback speed")
@app_commands.describe(mode="Use 'alt' to adjust audio speed instead of video speed", attachment="Video file to sync")
async def syncaudio_command(ctx: commands.Context, mode: str = "", attachment: discord.Attachment = None):
    """Sync video and audio durations.

    Default: adjusts video speed to match audio.
    Alt mode: adjusts audio speed to match video.

    Usage:
      g!syncaudio         — adjust video speed to match audio
      g!syncaudio alt     — adjust audio speed to match video
      g!sa                — alias
      g!sync alt          — alias
    """
    alt_mode = mode.lower().strip() == "alt"

    # Resolve attachment: slash commands pass it as a parameter;
    # prefix commands need us to look at the message or referenced message.
    if attachment is None:
        if ctx.message and ctx.message.attachments:
            attachment = ctx.message.attachments[0]
        elif ctx.message and ctx.message.reference:
            try:
                ref = await ctx.channel.fetch_message(ctx.message.reference.message_id)
                if ref.attachments:
                    attachment = ref.attachments[0]
            except Exception:
                pass

    if not attachment:
        mode_desc = "adjusts **video speed** to match audio" if not alt_mode else "adjusts **audio speed** to match video"
        await ctx.reply(
            "**IHTX Syncaudio**\n"
            f"Attach a video and use `g!syncaudio [alt]`.\n\n"
            f"Default: {mode_desc}\n"
            "Alt mode (`alt`): adjusts the other stream instead.\n\n"
            "Examples:\n"
            "```\n"
            "g!syncaudio         — video speed → match audio\n"
            "g!syncaudio alt     — audio speed → match video\n"
            "```\n"
            "Aliases: `g!sa`, `g!sync`"
        )
        return

    if attachment.size > MAX_FILE_SIZE:
        await ctx.reply(f"File too large (max 25 MB). Your file is {attachment.size / 1024 / 1024:.1f} MB.")
        return

    suffix = Path(attachment.filename).suffix.lower()
    if suffix not in VIDEO_EXTENSIONS:
        await ctx.reply(f"Syncaudio requires a video file. Got `{suffix}`.")
        return

    mode_label = "alt (audio→video)" if alt_mode else "default (video→audio)"
    status_msg = await ctx.reply(
        f"⚙️ Running **syncaudio** ({mode_label})... this may take a moment."
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, f"input{suffix}")
        output_path = os.path.join(tmpdir, "output_syncaudio.mp4")

        try:
            await download_attachment(attachment, input_path)
        except Exception as e:
            await status_msg.edit(content=f"❌ Failed to download your file: {e}")
            return

        loop = asyncio.get_event_loop()
        ok, info = await loop.run_in_executor(
            None, _run_syncaudio,
            input_path, output_path, alt_mode
        )

        if not ok:
            await status_msg.edit(content=f"❌ Syncaudio failed:\n```\n{info[-1500:]}\n```")
            return

        out_size = os.path.getsize(output_path)
        if out_size > MAX_FILE_SIZE:
            await status_msg.edit(content="❌ Output file too large for Discord (>25 MB). Try a shorter clip.")
            return

        out_filename = f"syncaudio_{Path(attachment.filename).stem}.mp4"
        try:
            await ctx.reply(
                content=f"✅ **IHTX syncaudio** ({mode_label}) applied!\n```\n{info}\n```",
                file=discord.File(output_path, filename=out_filename),
            )
            await status_msg.delete()
        except discord.HTTPException as e:
            await status_msg.edit(content=f"❌ Failed to upload result: {e}")

@bot.hybrid_command(name="presets", aliases=["effects", "list"], description="List all available IHTX presets")
async def presets_command(ctx: commands.Context):
    """List all available IHTX presets."""
    lines = [f"`{name}` — {PRESET_FILTERS[name]['vf'] or PRESET_FILTERS[name]['complex']}" for name in sorted(PRESET_FILTERS)]
    embed = discord.Embed(
        title="IHTX Bot — Available Presets",
        description="\n".join(lines),
        color=discord.Color.red(),
    )
    embed.add_field(
        name="Usage",
        value="Attach a video or image and run:\n`g!ihtx [preset]`\n\nDefault preset: `chaos`",
        inline=False,
    )
    embed.set_footer(text="I Hate The X — FFmpeg logo destruction bot")
    await ctx.reply(embed=embed)


@bot.hybrid_command(name="ihtxhelp", aliases=["bothelp"], description="Show IHTX Bot help and effect list")
async def help_command(ctx: commands.Context):
    embed = discord.Embed(
        title="IHTX Bot — Help",
        color=discord.Color.dark_red(),
    )
    embed.add_field(
        name="g!ihtx [preset]",
        value="Apply a preset effect to an attached video/image.\nDefault preset: `chaos`",
        inline=False,
    )
    embed.add_field(
        name="g!ihtx effect=value,effect=value [rep] [dur]",
        value="Chain custom effects. Params use `=`, sub-params use `;`.\n"
              "Example: `g!ihtx 10 0.5 - mp4 default huehsv 0.5;negate;multipitch=1|6|7`",
        inline=False,
    )
    embed.add_field(
        name="g!preview1280 [start] [dur]",
        value="12-segment TV-simulator montage.\nDefaults: start=1.85, dur=0.85",
        inline=False,
    )
    embed.add_field(
        name="g!multipitch <pitches>  (aliases: mp, multi)",
        value="Multi-voice pitch shift via SoX pitch.\n"
              "Pipe-separated semitones: `g!multipitch 25|5|8.5`\n"
              "Can also be chained: `g!ihtx huehsv 0.5;negate;multipitch=25|5|8.5`",
        inline=False,
    )
    embed.add_field(
        name="g!presets",
        value="List all available effect presets.",
        inline=False,
    )
    # Effect reference
    video_effects = (
        "hflip, vflip, negate (invert alias), invlum, invertrgb=r;g;b, grayscale, sepia, "
        "rotate=<deg>, huehsv=<val> (magick-style), ccshue=<val> (FFmpeg hue=h=), "
        "brightness=<val>, contrast=<val>, saturation=<val 0-1>, swapuv, gm4, realgm4"
    )
    distortion_effects = (
        "pinch&punch|p&p=strength;radius;cx;cy, swirl=angle;radius;cx;cy;fallout;lockaspectratio, tvsim=line_sync;zoom_grill;vertical, "
        "zoom=<amount>, mirror=<degrees>, gm91deform"
    )
    audio_effects = "multipitch=<semitones> (pipe-sep: 25|5|8.5), volume=<val>, vibrato=freq;depth, areverse, syncaudio[=alt]"
    lut_effects = "lut=<url>, invlum, ffmpeg(<raw args>)"

    embed.add_field(
        name="g!huehsv <hue>",
        value="Apply hue shift via ImageMagick haldclut.\n"
              "Example: `g!huehsv 0.5`\n"
              "Aliases: `g!hhsv`",
        inline=False,
    )
    embed.add_field(name="Video Effects", value=video_effects, inline=False)
    embed.add_field(name="Distortion", value=distortion_effects, inline=False)
    embed.add_field(name="Audio", value=audio_effects, inline=False)
    embed.add_field(
        name="g!syncaudio [alt]",
        value="Sync video & audio durations by adjusting playback speed.\n"
              "Default: video speed → match audio. `alt`: audio speed → match video.\n"
              "Aliases: `g!sa`, `g!sync`",
        inline=False,
    )
    embed.add_field(
        name="g!tvsim [line_sync] [zoom_grill] [vertical]",
        value="TV simulator with displacement mapping.\n"
              "Defaults: line_sync=0.25, zoom=1, vertical=False\n"
              "Aliases: `g!tv`",
        inline=False,
    )
    embed.add_field(name="LUT/Raw", value=lut_effects, inline=False)

    embed.add_field(
        name="Supported formats",
        value=", ".join(sorted(SUPPORTED_EXTENSIONS)),
        inline=False,
    )
    embed.add_field(
        name="Max file size",
        value=f"{MAX_FILE_SIZE // (1024*1024)} MB",
        inline=False,
    )
    embed.set_footer(text="I Hate The X — FFmpeg logo destruction bot")
    await ctx.reply(embed=embed)


# ---------- TV Simulator (tvsim) ----------

def _run_tvsim(
    input_path: str,
    output_path: str,
    line_sync: float = 0.25,
    zoom_grill: float = 1.0,
    vertical: bool = False,
) -> tuple[bool, str]:
    """Apply TV simulator effect using displacement map.

    Parameters:
      line_sync: 0-1, controls the scanline sync effect. Default 0.25.
      zoom_grill: scales the displacement map crop. Default 1.0.
      vertical: True = vertical orientation, False = horizontal.
    """
    line_sync = max(0.0, min(1.0, line_sync))
    zoom_grill = max(0.1, zoom_grill)
    contrast = (1 - line_sync) * 2.366666

    # Download displacement map if needed
    disp_url = "https://file.garden/aTXso15ukD3mnuPI/tv_sim_displacement_map.mov"
    disp_path = os.path.join(os.path.dirname(output_path), "disp_map.mov")
    if not os.path.exists(disp_path):
        try:
            import urllib.request
            urllib.request.urlretrieve(disp_url, disp_path)
        except Exception:
            return False, f"Failed to download displacement map from {disp_url}"

    # Get input dimensions
    w, h = 0, 0
    info = _ffprobe_video_info(input_path)
    w, h = info.get("width", 0), info.get("height", 0)
    if w == 0 or h == 0:
        return False, "Could not read input dimensions."

    # Build filter_complex
    if vertical:
        # Vertical mode: [00][y][x]displace
        fc = (
            f"[0]scale=854:854,format=bgr32[00];"
            f"[1]crop=iw:ih/{zoom_grill}:0:0,transpose=2,scale=854:854,"
            f"eq=contrast={contrast},format=bgr32,hue=b=-0.033[x];"
            f"color=s=854x854:c=#808080,format=bgr32[y];"
            f"[00][y][x]displace=edge=wrap,scale={w}:{h},setsar=1,format=yuv444p"
        )
    else:
        # Horizontal mode: [00][x][y]displace
        fc = (
            f"[0]scale=854:854,format=bgr32[00];"
            f"[1]crop=iw:ih/{zoom_grill}:0:0,scale=854:854,"
            f"eq=contrast={contrast},format=bgr32,hue=b=-0.033[x];"
            f"color=s=854x854:c=#808080,format=bgr32[y];"
            f"[00][x][y]displace=edge=wrap,scale={w}:{h},setsar=1,format=yuv444p"
        )

    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-stream_loop", "-1", "-i", disp_path,
        "-filter_complex", fc,
        "-shortest", "-map", "0:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", output_path,
    ]
    return _run_ffmpeg_raw(cmd, timeout=300)


@bot.hybrid_command(name="tvsim", aliases=["tv"], description="Apply TV simulator displacement effect")
@app_commands.describe(
    line_sync="Scanline sync value 0-1 (default: 0.25)",
    zoom_grill="Zoom grill factor (default: 1)",
    vertical="Use vertical orientation (default: False)",
    attachment="Video to apply TV simulator effect"
)
async def tvsim_command(
    ctx: commands.Context,
    line_sync: float = 0.25,
    zoom_grill: float = 1.0,
    vertical: bool = False,
    attachment: discord.Attachment = None,
):
    """Apply TV simulator effect to an attached video.

    Usage:
      g!tvsim                     — default settings (0.25, 1, False)
      g!tvsim 0.5 2 True          — custom: line_sync=0.5, zoom=2, vertical
      g!tvsim 0.75                — line_sync=0.75, defaults for rest
    """
    # Resolve attachment
    if attachment is None:
        if ctx.message and ctx.message.attachments:
            attachment = ctx.message.attachments[0]
        elif ctx.message and ctx.message.reference:
            try:
                ref = await ctx.channel.fetch_message(ctx.message.reference.message_id)
                if ref.attachments:
                    attachment = ref.attachments[0]
            except Exception:
                pass

    if not attachment:
        await ctx.reply(
            "**IHTX TV Simulator**\n"
            "Attach a video and use `g!tvsim [line_sync] [zoom_grill] [vertical]`.\n\n"
            "Parameters:\n"
            "- `line_sync`: 0-1 (default: 0.25)\n"
            "- `zoom_grill`: factor (default: 1)\n"
            "- `vertical`: True/False (default: False)\n\n"
            "Example: `g!tvsim 0.5 2 True`\n"
            "Aliases: `g!tv`"
        )
        return

    if attachment.size > MAX_FILE_SIZE:
        await ctx.reply(f"File too large (max 25 MB). Your file is {attachment.size / 1024 / 1024:.1f} MB.")
        return

    suffix = Path(attachment.filename).suffix.lower()
    if suffix not in VIDEO_EXTENSIONS:
        await ctx.reply(f"TV Simulator requires a video file. Got `{suffix}`.")
        return

    # Clamp values
    if line_sync < 0 or line_sync > 1:
        await ctx.reply("`line_sync` must be between 0 and 1.")
        return

    status_msg = await ctx.reply(
        f"\u2699\ufe0f Applying **TV Simulator** (sync={line_sync}, zoom={zoom_grill}, vertical={vertical})..."
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, f"input{suffix}")
        output_path = os.path.join(tmpdir, "output_tvsim.mp4")

        try:
            await download_attachment(attachment, input_path)
        except Exception as e:
            await status_msg.edit(content=f"\u274c Failed to download your file: {e}")
            return

        loop = asyncio.get_event_loop()
        ok, err = await loop.run_in_executor(
            None, _run_tvsim, input_path, output_path, line_sync, zoom_grill, vertical
        )

        if not ok:
            await status_msg.edit(content=f"\u274c TV Simulator failed:\n```\n{err[-1500:]}\n```")
            return

        out_size = os.path.getsize(output_path)
        if out_size > MAX_FILE_SIZE:
            await status_msg.edit(content="\u274c Output file too large for Discord (>25 MB). Try a shorter clip.")
            return

        out_filename = f"tvsim_{line_sync}_{zoom_grill}_{vertical}_{Path(attachment.filename).stem}.mp4"
        try:
            await ctx.reply(
                content=f"\u2705 **IHTX TV Simulator** (sync={line_sync}, zoom={zoom_grill}, vertical={vertical}) applied!",
                file=discord.File(output_path, filename=out_filename),
            )
            await status_msg.delete()
        except discord.HTTPException as e:
            await status_msg.edit(content=f"\u274c Failed to upload result: {e}")


# ---------- Owner-only moderation / utility commands ----------

def _parse_digits(s: str) -> int:
    """Extract numeric ID from mention or plain id string."""
    if not s:
        raise ValueError("No id provided")
    m = re.search(r"(\d{6,20})", s)
    if m:
        return int(m.group(1))
    try:
        return int(s)
    except Exception:
        raise ValueError("Could not parse id")


@bot.hybrid_command(name="blockuser", description="Owner-only: add a user to the blocklist")
@app_commands.describe(user="User mention or ID to block")
@commands.check(_is_owner)
async def blockuser(ctx: commands.Context, user: str):
    """Owner-only: add a user ID or mention to the user blocklist."""
    try:
        user_id = _parse_digits(user)
    except ValueError:
        await ctx.reply("❌ Invalid user. Provide a mention or numeric ID.")
        return
    if user_id in blocklist:
        await ctx.reply(f"User `{user_id}` is already blocked.")
        return
    blocklist.add(user_id)
    _save_blocklist()
    await ctx.reply(f"✅ Blocked user `{user_id}`.")


@bot.hybrid_command(name="unblockuser", description="Owner-only: remove a user from the blocklist")
@app_commands.describe(user="User mention or ID to unblock")
@commands.check(_is_owner)
async def unblockuser(ctx: commands.Context, user: str):
    """Owner-only: remove a user ID or mention from the user blocklist."""
    try:
        user_id = _parse_digits(user)
    except ValueError:
        await ctx.reply("❌ Invalid user. Provide a mention or numeric ID.")
        return
    if user_id not in blocklist:
        await ctx.reply(f"User `{user_id}` is not blocked.")
        return
    blocklist.discard(user_id)
    _save_blocklist()
    await ctx.reply(f"✅ Unblocked user `{user_id}`.")


@bot.hybrid_command(name="blockchannel", description="Owner-only: add a channel to the blocklist")
@app_commands.describe(channel="Channel mention or ID to block (omit for current channel)")
@commands.check(_is_owner)
async def blockchannel(ctx: commands.Context, channel: str = None):
    """Owner-only: add a channel to the channel blocklist. If omitted, blocks current channel."""
    if channel is None:
        channel_id = ctx.channel.id
    else:
        try:
            channel_id = _parse_digits(channel)
        except ValueError:
            await ctx.reply("❌ Invalid channel. Provide a channel mention or numeric ID.")
            return
    if channel_id in channel_blocks:
        await ctx.reply(f"Channel `{channel_id}` is already blocked.")
        return
    channel_blocks.add(channel_id)
    _save_channel_blocks()
    await ctx.reply(f"✅ Blocked channel `{channel_id}`.")


@bot.hybrid_command(name="unblockchannel", description="Owner-only: remove a channel from the blocklist")
@app_commands.describe(channel="Channel mention or ID to unblock (omit for current channel)")
@commands.check(_is_owner)
async def unblockchannel(ctx: commands.Context, channel: str = None):
    """Owner-only: remove a channel from the channel blocklist. If omitted, unblocks current channel."""
    if channel is None:
        channel_id = ctx.channel.id
    else:
        try:
            channel_id = _parse_digits(channel)
        except ValueError:
            await ctx.reply("❌ Invalid channel. Provide a channel mention or numeric ID.")
            return
    if channel_id not in channel_blocks:
        await ctx.reply(f"Channel `{channel_id}` is not blocked.")
        return
    channel_blocks.discard(channel_id)
    _save_channel_blocks()
    await ctx.reply(f"✅ Unblocked channel `{channel_id}`.")


@bot.hybrid_command(name="say", description="Owner-only: make the bot send a message")
@app_commands.describe(message="Message content to send")
@commands.check(_is_owner)
async def say(ctx: commands.Context, *, message: str):
    """Owner-only: make the bot send a plain message in the current channel."""
    try:
        await ctx.send(message)
        if ctx.message:
            await ctx.message.add_reaction("✅")
    except Exception as e:
        await ctx.reply(f"❌ Failed to send message: {e}")


@bot.hybrid_command(name="sayembed", description="Owner-only: make the bot send an embed")
@app_commands.describe(content="Embed content (use | to split title|description)")
@commands.check(_is_owner)
async def sayembed(ctx: commands.Context, *, content: str):
    """
    Owner-only: send an embed.
    If `content` contains a '|' it will split into title|description, otherwise content is used as description.
    Example:
      g!sayembed Title | This is the embed body
    """
    try:
        if "|" in content:
            title, desc = [p.strip() for p in content.split("|", 1)]
        else:
            title = ""
            desc = content
        emb = discord.Embed(title=title or None, description=desc or None, color=discord.Color.dark_red())
        await ctx.send(embed=emb)
        if ctx.message:
            await ctx.message.add_reaction("✅")
    except Exception as e:
        await ctx.reply(f"❌ Failed to send embed: {e}")


# ---------- Error handling & run ----------

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply(f"Missing argument: `{error.param.name}`. Use `g!ihtxhelp` for usage.")
        return
    # Permission errors from owner-only commands
    if isinstance(error, commands.CheckFailure):
        # If check failed for owner-only commands, be quiet (or you could notify)
        return
    raise error


if __name__ == "__main__":
    if not TOKEN:
        print("ERROR: DISCORD_TOKEN environment variable not set.", file=sys.stderr)
        sys.exit(1)
    bot.run(TOKEN)
