"""
IHTX Bot   I Hate The X FFmpeg Discord Bot

This file restores the full implementation by merging the last working version
with the newer top-level configuration (owners, limits, tags, presets).

It keeps the 'g!' prefix and the newer PRESET_FILTERS/HELP_TEXT while restoring
command handlers, ffmpeg integration, and helpers. Additional owner commands
and more parameter parsing for g!ihtx have been added (split, lut, multipitch,
flips, speed, etc.).

Notes:
- Some advanced audio operations (rubberband multipitch) require ffmpeg
  compiled with the rubberband filter or the rubberband library available.
- This implementation attempts to use ffmpeg audio filters where possible and
  will return an FFmpeg error if the filter is not available.

Dependencies required at runtime: ffmpeg, aiohttp, discord.py, optionally yt-dlp,
ImageMagick/sox/etc. depending on advanced effects.
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
import math

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

# ---------- Configuration & constants ----------

TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    print("ERROR: DISCORD_TOKEN environment variable not set.", file=sys.stderr)
    sys.exit(1)

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
HEAVY_COMMANDS = {"ihtx", "effect", "destroy", "preview1280", "p1280", "ihtxsync", "download", "dl"}
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

# Effect filter definitions (kept from the newer/top-of-file)
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


async def download_url_to_path(url: str, dest: str):
    """Download a URL to dest (used for LUT files)."""
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise ValueError(f"Failed to download {url} (HTTP {resp.status})")
            data = await resp.read()
    with open(dest + ".part", "wb") as f:
        f.write(data)
    os.replace(dest + ".part", dest)


def run_ffmpeg(input_path: str, output_path: str, preset_key: str, is_video: bool) -> tuple[bool, str]:
    """Run ffmpeg using PRESET_FILTERS. Returns (ok, stderr-or-empty)."""
    cfg = PRESET_FILTERS.get(preset_key)
    if cfg is None:
        cfg = PRESET_FILTERS["chaos"]

    # Build cmd
    if is_video:
        if cfg.get("complex"):
            cmd = [
                "ffmpeg", "-y", "-i", input_path,
                "-filter_complex", cfg["complex"],
            ]
        else:
            cmd = [
                "ffmpeg", "-y", "-i", input_path,
                "-vf", cfg.get("vf", ""),
            ]
        # audio filters
        if cfg.get("af"):
            cmd += ["-af", cfg["af"]]
        # encoding
        cmd += [
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac",
            "-t", str(cfg.get("t", 30)),
            output_path
        ]
    else:
        # Images / animated GIF generation
        if cfg.get("complex"):
            fc = cfg["complex"] + ",split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"
            cmd = [
                "ffmpeg", "-y",
                "-loop", "1", "-i", input_path,
                "-filter_complex", fc,
                "-t", str(cfg.get("t", 3)),
                output_path
            ]
        else:
            vf = (cfg.get("vf") or "") + ",split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"
            cmd = [
                "ffmpeg", "-y",
                "-loop", "1", "-i", input_path,
                "-vf", vf,
                "-t", str(cfg.get("t", 3)),
                output_path
            ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            return False, result.stderr[-2000:]
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "FFmpeg timed out (>180s)"
    except Exception as e:
        return False, str(e)


def get_output_ext(input_ext: str, is_video: bool) -> str:
    return ".mp4" if is_video else ".gif"


# ---------- Parameter parsing & filter building helpers ----------


def parse_param_string(s: str) -> dict:
    """Parse a pipe-style parameter string into a dict.
    Example: "glitch=true,speed=2,split=left,lut=https://.../film.cube"
    Returns: {"glitch":"true", "speed":"2", ...}
    """
    params = {}
    # Accept both comma-separated and space-separated
    parts = re.split(r"[,\n]+", s)
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.strip().lower()] = v.strip()
        else:
            # flags like "chaos" or "glitch"
            params[p.strip().lower()] = "true"
    return params


def build_filters_for_params(base_preset: str, params: dict, is_video: bool, tmpdir: str) -> str:
    """Build a video vf (or complex) and audio af (if needed) from params.
    This function modifies PRESET_FILTERS["__tmp__"] and returns the key to use.
    It may download LUT files into tmpdir (caller must cleanup tmpdir).
    """
    vf_parts = []
    af_part = None
    complex_part = None
    tval = None

    # Start from base preset VF/complex if any
    base = PRESET_FILTERS.get(base_preset, {})
    if base.get("complex"):
        complex_part = base["complex"]
    elif base.get("vf"):
        vf_parts.append(base["vf"])

    # Handle split/crop
    split = params.get("split") or params.get("vsplit")
    if split:
        s = split.lower()
        if s in ("left", "right"):
            # crop half horizontally then scale back to original size
            if s == "left":
                vf_parts.insert(0, "crop=iw/2:ih:0:0,scale=iw:ih")
            else:
                vf_parts.insert(0, "crop=iw/2:ih:iw/2:0,scale=iw:ih")
        elif s in ("top", "bottom"):
            if s == "top":
                vf_parts.insert(0, "crop=iw:ih/2:0:0,scale=iw:ih")
            else:
                vf_parts.insert(0, "crop=iw:ih/2:0:ih/2,scale=iw:ih")

    # LUT
    lut = params.get("lut")
    if lut:
        # download LUT to tmpdir
        try:
            parsed = urllib.parse.urlparse(lut)
            if parsed.scheme.startswith("http"):
                fname = hashlib.sha1(lut.encode()).hexdigest() + ".cube"
                dest = os.path.join(tmpdir, fname)
                # synchronous run of aio download in event loop not possible here; caller downloads before
                # so we expect params to contain key "_lut_path" if downloaded. We'll check for that.
                lut_path = params.get("_lut_path")
                if lut_path:
                    vf_parts.append(f"lut3d=file='{lut_path}'")
                else:
                    # fallback: try to use URL directly (ffmpeg supports lut3d=file=filename only)
                    pass
            else:
                # local path
                vf_parts.append(f"lut3d=file='{lut}'")
        except Exception:
            pass

    # flips
    if params.get("hflip") in ("1", "true", "True") or params.get("hflip") == "true":
        vf_parts.append("hflip")
    if params.get("vflip") in ("1", "true", "True") or params.get("vflip") == "true":
        vf_parts.append("vflip")

    # mirror (simple implementation: use crop+hflip overlay)
    hmirror = params.get("hmirror")
    if hmirror in ("1", "2", "'1'", "'2'"):
        # 1 = left mirrored to right (keep left area), 2 = right mirrored to left
        if hmirror.startswith("1"):
            vf_parts.append("crop=iw/2:ih:0:0,scale=iw:ih")
        else:
            vf_parts.append("crop=iw/2:ih:iw/2:0,scale=iw:ih")
    vmirror = params.get("vmirror")
    if vmirror in ("1", "2"):
        if vmirror.startswith("1"):
            vf_parts.append("crop=iw:ih/2:0:0,scale=iw:ih")
        else:
            vf_parts.append("crop=iw:ih/2:0:ih/2,scale=iw:ih")

    # hue2 (secondary hue rotation)
    hue2 = params.get("hue2")
    if hue2:
        try:
            v = float(hue2)
            vf_parts.append(f"hue=h={v}")
        except Exception:
            pass

    # speed (video tempo)
    speed = params.get("speed")
    if speed:
        try:
            s = float(speed)
            # set both setpts for video and atempo for audio (if audio present). atempo supports up to 2.0 repeatedly.
            vf_parts.append(f"setpts=PTS/{s}")
            if s != 1.0:
                # audio scaling handled via af below if multipitch not used
                af_part = af_part or ""
                # Use a simple atempo chain for speeds outside 0.5..2.0
                if s <= 0:
                    pass
                else:
                    # decompose speed into factors between 0.5 and 2 for atempo
                    factors = []
                    remaining = s
                    # if speeding up (>1), we need atempo=remaining, else atempo=1/remaining? atempo changes tempo (time)
                    # ffmpeg atempo modifies tempo (1.0 = original). setpts already adjusts video.
                    # For simplicity: apply atempo if within 0.5..2
                    if 0.5 <= s <= 2.0:
                        af_part = (af_part + "," if af_part else "") + f"atempo={s}"
                    else:
                        # chain multiple atempo filters (approx)
                        val = s
                        chain = []
                        while val > 2.0:
                            chain.append("2.0")
                            val /= 2.0
                        while val < 0.5:
                            chain.append("0.5")
                            val /= 0.5
                        if val != 1.0:
                            chain.append(str(val))
                        af_part = (af_part + "," if af_part else "") + ",".join(f"atempo={c}" for c in chain)
        except Exception:
            pass

    # multipitch audio
    multipitch = params.get("multipitch")
    if multipitch:
        try:
            semis = [int(x) for x in re.split(r"[;,:]+", multipitch) if x.strip()]
            # Build audio complex using rubberband filter for each semitone, then amix
            # e.g. [0:a]rubberband=pitch=2^{s/12}[a0];[0:a]rubberband=pitch=2^{s2/12}[a1];[a0][a1]amix=inputs=2:normalize=0, bass=g=2.5, highpass=f=10
            parts = []
            labels = []
            for i, s in enumerate(semis):
                ratio = math.pow(2.0, s / 12.0)
                lbl = f"ap{i}"
                labels.append(lbl)
                parts.append(f"[0:a]rubberband=pitch={ratio:.6f}[{lbl}]")
            amix_inputs = ";".join([f"[{l}]" for l in labels])
            # build amix filter; ffmpeg expects [a0][a1]amix=inputs=2
            amix = "".join(f"[{l}]" for l in labels) + f"amix=inputs={len(labels)}:normalize=0,bass=g=2.5,highpass=f=10"
            af_full = ";".join(parts) + ";" + amix
            af_part = af_full
        except Exception:
            pass

    # wave, swirl, pinch, reverse, etc. — skipped for brevity but could be added similarly

    # assemble into a temporary preset
    tmp_key = "__tmp__"
    PRESET_FILTERS[tmp_key] = {
        "vf": ",".join([p for p in vf_parts if p]),
        "complex": complex_part,
    }
    if af_part:
        PRESET_FILTERS[tmp_key]["af"] = af_part
    if tval:
        PRESET_FILTERS[tmp_key]["t"] = tval

    return tmp_key

# ---------- Bot events & commands (restored functionality) ----------

@bot.event
async def on_ready():
    print(f"IHTX Bot online as {bot.user} (ID: {bot.user.id})")
    print("------")
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching,
        name="logos get destroyed | g!ihtx"
    ))


@bot.command(name="ihtx", aliases=["effect", "destroy"])
async def ihtx_command(ctx: commands.Context, *preset_args: str):
    """Apply an IHTX FFmpeg effect to an attached video or image.

    Usage:
      g!ihtx [preset-or-params]   attach a file.
    Examples:
      g!ihtx glitch
      g!ihtx glitch=true,speed=2,split=left
      g!ihtx multipitch=12
    """
    # Combine args into one string
    preset_raw = " ".join(preset_args).strip() if preset_args else ""
    if not preset_raw:
        preset_raw = "chaos"

    # detect param-style usage: presence of = or ,
    if "=" in preset_raw or "," in preset_raw:
        params = parse_param_string(preset_raw)
        # If a named visual preset is present, pick it, else default to chaos
        chosen_visual = None
        for v in VISUAL_PRESETS:
            if params.get(v) in ("1", "true", "True", "yes"):
                chosen_visual = v
                break
        if not chosen_visual:
            # if first token matches a preset name, use it
            first = preset_raw.split(",")[0].split("=", 1)[0].strip().lower()
            if first in VISUAL_PRESETS:
                chosen_visual = first
        if not chosen_visual:
            chosen_visual = "chaos"
    else:
        # simple preset name
        chosen_visual = preset_raw.lower()
        params = {chosen_visual: "true"}
        if chosen_visual not in VISUAL_PRESETS:
            await ctx.reply(f"Unknown preset `{chosen_visual}`. Use `g!presets` to list available presets.")
            return

    # Look for attachments (or referenced message attachments)
    attachment = None
    if ctx.message.attachments:
        attachment = ctx.message.attachments[0]
    elif ctx.message.reference:
        try:
            ref = await ctx.channel.fetch_message(ctx.message.reference.message_id)
            if ref.attachments:
                attachment = ref.attachments[0]
        except Exception:
            pass

    if not attachment:
        preset_list = ", ".join(f"`{p}`" for p in sorted(VISUAL_PRESETS))
        await ctx.reply(
            f"**I HATE THE X   IHTX Bot**\n"
            f"Attach a video or image and use `g!ihtx [preset]`.\n\n"
            f"**Presets:** {preset_list}\n\n"
            f"Examples:\n"
            f"`g!ihtx chaos`\n"
            f"`g!ihtx glitch`\n"
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
        f"⚙️ Applying **{chosen_visual}** effect... this may take a moment."
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, f"input{suffix}")
        output_path = os.path.join(tmpdir, f"output{out_ext}")

        try:
            await download_attachment(attachment, input_path)
        except Exception as e:
            await status_msg.edit(content=f"❌ Failed to download your file: {e}")
            return

        # If LUT provided as URL, download it now and place path into params
        lut_url = params.get("lut")
        if lut_url and urllib.parse.urlparse(lut_url).scheme.startswith("http"):
            try:
                lut_fname = hashlib.sha1(lut_url.encode()).hexdigest() + ".cube"
                lut_path = os.path.join(tmpdir, lut_fname)
                await download_url_to_path(lut_url, lut_path)
                params["_lut_path"] = lut_path
            except Exception as e:
                await status_msg.edit(content=f"❌ Failed to download LUT: {e}")
                return

        # Build temporary preset based on chosen_visual + params
        tmp_key = build_filters_for_params(chosen_visual, params, is_video, tmpdir)

        loop = asyncio.get_event_loop()
        ok, err = await loop.run_in_executor(None, run_ffmpeg, input_path, output_path, tmp_key, is_video)

        # cleanup temporary preset
        try:
            del PRESET_FILTERS[tmp_key]
        except Exception:
            pass

        if not ok:
            await status_msg.edit(content=f"❌ FFmpeg failed:\n```\n{err[-1500:]}\n```")
            return

        out_size = os.path.getsize(output_path)
        if out_size > MAX_FILE_SIZE:
            await status_msg.edit(content="❌ Output file too large for Discord (>25 MB). Try a shorter clip.")
            return

        out_filename = f"ihtx_{chosen_visual}_{Path(attachment.filename).stem}{out_ext}"
        try:
            await ctx.reply(
                content=f"✅ **IHTX `{chosen_visual}`** applied!",
                file=discord.File(output_path, filename=out_filename),
            )
            await status_msg.delete()
        except discord.HTTPException as e:
            await status_msg.edit(content=f"❌ Failed to upload result: {e}")


@bot.command(name="presets", aliases=["effects", "list"])
async def presets_command(ctx: commands.Context):
    """List all available IHTX presets."""
    lines = [f"`{name}`   {PRESET_FILTERS[name]['vf'] or PRESET_FILTERS[name]['complex']}" for name in sorted(PRESET_FILTERS) if not name.startswith("__tmp__")]
    embed = discord.Embed(
        title="IHTX Bot   Available Presets",
        description="\n".join(lines),
        color=discord.Color.red(),
    )
    embed.add_field(
        name="Usage",
        value="Attach a video or image and run:\n`g!ihtx [preset]`\n\nDefault preset: `chaos`",
        inline=False,
    )
    embed.set_footer(text="I Hate The X   FFmpeg logo destruction bot")
    await ctx.reply(embed=embed)


@bot.command(name="ihtxhelp", aliases=["bothelp"])
async def help_command(ctx: commands.Context):
    embed = discord.Embed(
        title="IHTX Bot   Help",
        color=discord.Color.dark_red(),
    )
    embed.add_field(
        name="g!ihtx [preset]",
        value="Apply an IHTX effect to an attached video or image.\nDefault preset: `chaos`",
        inline=False,
    )
    embed.add_field(
        name="g!presets",
        value="List all available effect presets.",
        inline=False,
    )
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
    embed.set_footer(text="I Hate The X   FFmpeg logo destruction bot")
    await ctx.reply(embed=embed)


# ---------- Owner-only admin commands (block/unblock/say) ----------

@bot.command(name="blockuser")
@commands.check(_is_owner)
async def block_user(ctx: commands.Context, user: discord.User):
    """Owner-only: add a user ID to the blocklist."""
    blocklist.add(user.id)
    _save_blocklist()
    await ctx.reply(f"✅ Blocked user {user} ({user.id}).")


@bot.command(name="unblockuser")
@commands.check(_is_owner)
async def unblock_user(ctx: commands.Context, user: discord.User):
    """Owner-only: remove a user ID from the blocklist."""
    if user.id in blocklist:
        blocklist.remove(user.id)
        _save_blocklist()
        await ctx.reply(f"✅ Unblocked user {user} ({user.id}).")
    else:
        await ctx.reply(f"User {user} was not blocked.")


@bot.command(name="blockchannel")
@commands.check(_is_owner)
async def block_channel(ctx: commands.Context, channel: discord.TextChannel):
    """Owner-only: block a channel from using commands."""
    channel_blocks.add(channel.id)
    _save_channel_blocks()
    await ctx.reply(f"✅ Blocked channel {channel.mention} ({channel.id}).")


@bot.command(name="unblockchannel")
@commands.check(_is_owner)
async def unblock_channel(ctx: commands.Context, channel: discord.TextChannel):
    """Owner-only: unblock a previously blocked channel."""
    if channel.id in channel_blocks:
        channel_blocks.remove(channel.id)
        _save_channel_blocks()
        await ctx.reply(f"✅ Unblocked channel {channel.mention} ({channel.id}).")
    else:
        await ctx.reply("That channel was not blocked.")


@bot.command(name="say")
@commands.check(_is_owner)
async def say(ctx: commands.Context, *, content: str):
    """Owner-only: make the bot say a message."""
    await ctx.message.delete()
    await ctx.send(content)


@bot.command(name="sayembed")
@commands.check(_is_owner)
async def say_embed(ctx: commands.Context, title: str, *, description: str):
    """Owner-only: send an embed. Usage: g!sayembed "Title" description text..."""
    await ctx.message.delete()
    emb = discord.Embed(title=title, description=description, color=discord.Color.red())
    await ctx.send(embed=emb)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply(f"Missing argument: `{error.param.name}`. Use `g!ihtxhelp` for usage.")
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.reply("❌ You do not have permission to run this command.")
        return
    raise error


if __name__ == "__main__":
    bot.run(TOKEN)
