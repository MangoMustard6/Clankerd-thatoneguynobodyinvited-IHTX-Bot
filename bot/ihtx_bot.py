import discord
from discord.ext import commands
import asyncio
import os
import tempfile
import subprocess
import aiohttp
import random
import sys
from pathlib import Path

TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    print("ERROR: DISCORD_TOKEN environment variable not set.", file=sys.stderr)
    sys.exit(1)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

SUPPORTED_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".gif", ".png", ".jpg", ".jpeg", ".webp"}
MAX_FILE_SIZE = 25 * 1024 * 1024  # 25 MB

IHTX_PRESETS = {
    "chaos":     "extreme glitch + color explosion",
    "glitch":    "heavy datamosh glitch",
    "shake":     "violent shake + zoom pulse",
    "rainbow":   "chromatic aberration + hue spin",
    "static":    "TV static noise overlay",
    "melt":      "melt + distort",
    "corrupt":   "corrupt + scanlines",
}

def build_ffmpeg_filter(preset: str, is_video: bool) -> list[str]:
    """Return ffmpeg filter_complex or vf args for the given IHTX preset."""

    base_noise = "noise=alls=40:allf=t+u"
    shake = "crop=iw-20:ih-20:10+5*sin(t*30):10+5*cos(t*17),scale=iw+20:ih+20"
    chromab = (
        "[0:v]split=3[r][g][b];"
        "[r]lutrgb=r=val:g=0:b=0,pad=iw+6:ih:3:0[ro];"
        "[g]lutrgb=r=0:g=val:b=0[go];"
        "[b]lutrgb=r=0:g=0:b=val,pad=iw+6:ih:0:0[bo];"
        "[ro][go]blend=all_mode=addition[rg];"
        "[rg][bo]blend=all_mode=addition"
    )

    presets: dict[str, dict] = {
        "chaos": {
            "vf": f"{shake},{base_noise},hue=h=t*180:s=2,eq=contrast=1.5:brightness=0.05:saturation=3",
            "complex": None,
        },
        "glitch": {
            "vf": f"rgbashift=rh=8:rv=-8:gh=-4:gv=4:bh=6:bv=-6,{base_noise},eq=contrast=1.8:saturation=0",
            "complex": None,
        },
        "shake": {
            "vf": f"{shake},{base_noise},eq=contrast=1.3:saturation=1.5",
            "complex": None,
        },
        "rainbow": {
            "vf": None,
            "complex": chromab,
        },
        "static": {
            "vf": f"{base_noise},curves=vintage,eq=contrast=1.2",
            "complex": None,
        },
        "melt": {
            "vf": f"perspective=x0=0:y0=0:x1=iw:y1=20*sin(t*3):x2=0:y2=ih:x3=iw:y3=ih-20*sin(t*3),{base_noise}",
            "complex": None,
        },
        "corrupt": {
            "vf": f"drawgrid=x=0:y=0:w=iw:h=5:t=1:color=white@0.1,{base_noise},eq=gamma=1.5:saturation=0.3:contrast=2",
            "complex": None,
        },
    }

    cfg = presets.get(preset, presets["chaos"])
    return cfg


async def download_attachment(attachment: discord.Attachment, dest: str):
    async with aiohttp.ClientSession() as session:
        async with session.get(attachment.url) as resp:
            if resp.status != 200:
                raise ValueError(f"Failed to download attachment (HTTP {resp.status})")
            data = await resp.read()
    with open(dest, "wb") as f:
        f.write(data)


def run_ffmpeg(input_path: str, output_path: str, preset: str, is_video: bool) -> tuple[bool, str]:
    cfg = build_ffmpeg_filter(preset, is_video)

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
        # Image → animated GIF with the effect applied
        if cfg["complex"]:
            cmd = [
                "ffmpeg", "-y",
                "-loop", "1", "-i", input_path,
                "-filter_complex", cfg["complex"] + ",split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
                "-t", "3",
                output_path
            ]
        else:
            cmd = [
                "ffmpeg", "-y",
                "-loop", "1", "-i", input_path,
                "-vf", cfg["vf"] + ",split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
                "-t", "3",
                output_path
            ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            return False, result.stderr[-2000:]
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "FFmpeg timed out (>120s)"
    except Exception as e:
        return False, str(e)


def get_output_ext(input_ext: str, is_video: bool) -> str:
    if is_video:
        return ".mp4"
    return ".gif"


@bot.event
async def on_ready():
    print(f"IHTX Bot online as {bot.user} (ID: {bot.user.id})")
    print("------")
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching,
        name="logos get destroyed | !ihtx"
    ))


@bot.command(name="ihtx", aliases=["effect", "destroy"])
async def ihtx_command(ctx: commands.Context, preset: str = "chaos"):
    """Apply an IHTX FFmpeg effect to an attached video or image.

    Usage:
      !ihtx [preset]  — attach a file. Presets: chaos, glitch, shake, rainbow, static, melt, corrupt
    """
    preset = preset.lower()
    if preset not in IHTX_PRESETS:
        preset_list = ", ".join(f"`{p}`" for p in IHTX_PRESETS)
        await ctx.reply(
            f"Unknown preset. Available presets: {preset_list}\n"
            f"Example: `!ihtx glitch` (attach a video or image)"
        )
        return

    if not ctx.message.attachments:
        preset_list = ", ".join(f"`{p}`" for p in IHTX_PRESETS)
        await ctx.reply(
            f"**I HATE THE X — IHTX Bot**\n"
            f"Attach a video or image and use `!ihtx [preset]`.\n\n"
            f"**Presets:** {preset_list}\n\n"
            f"Examples:\n"
            f"`!ihtx chaos` — {IHTX_PRESETS['chaos']}\n"
            f"`!ihtx glitch` — {IHTX_PRESETS['glitch']}\n"
            f"`!ihtx rainbow` — {IHTX_PRESETS['rainbow']}\n"
        )
        return

    attachment = ctx.message.attachments[0]

    if attachment.size > MAX_FILE_SIZE:
        await ctx.reply(f"File too large (max 25 MB). Your file is {attachment.size / 1024 / 1024:.1f} MB.")
        return

    suffix = Path(attachment.filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        await ctx.reply(f"Unsupported file type `{suffix}`. Supported: {', '.join(SUPPORTED_EXTENSIONS)}")
        return

    is_video = suffix in {".mp4", ".mov", ".avi", ".mkv", ".webm", ".gif"}
    out_ext = get_output_ext(suffix, is_video)

    status_msg = await ctx.reply(
        f"⚙️ Applying **{preset}** effect ({IHTX_PRESETS[preset]})... this may take a moment."
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
            None, run_ffmpeg, input_path, output_path, preset, is_video
        )

        if not ok:
            await status_msg.edit(content=f"❌ FFmpeg failed:\n```\n{err[-1500:]}\n```")
            return

        out_size = os.path.getsize(output_path)
        if out_size > 25 * 1024 * 1024:
            await status_msg.edit(content="❌ Output file too large for Discord (>25 MB). Try a shorter clip.")
            return

        out_filename = f"ihtx_{preset}_{Path(attachment.filename).stem}{out_ext}"
        try:
            await ctx.reply(
                content=f"✅ **IHTX `{preset}`** applied!",
                file=discord.File(output_path, filename=out_filename),
            )
            await status_msg.delete()
        except discord.HTTPException as e:
            await status_msg.edit(content=f"❌ Failed to upload result: {e}")


@bot.command(name="presets", aliases=["effects", "list"])
async def presets_command(ctx: commands.Context):
    """List all available IHTX presets."""
    lines = [f"`{name}` — {desc}" for name, desc in IHTX_PRESETS.items()]
    embed = discord.Embed(
        title="IHTX Bot — Available Presets",
        description="\n".join(lines),
        color=discord.Color.red(),
    )
    embed.add_field(
        name="Usage",
        value="Attach a video or image and run:\n`!ihtx [preset]`\n\nDefault preset: `chaos`",
        inline=False,
    )
    embed.set_footer(text="I Hate The X — FFmpeg logo destruction bot")
    await ctx.reply(embed=embed)


@bot.command(name="ihtxhelp", aliases=["bothelp"])
async def help_command(ctx: commands.Context):
    embed = discord.Embed(
        title="IHTX Bot — Help",
        color=discord.Color.dark_red(),
    )
    embed.add_field(
        name="!ihtx [preset]",
        value="Apply an IHTX effect to an attached video or image.\nDefault preset: `chaos`",
        inline=False,
    )
    embed.add_field(
        name="!presets",
        value="List all available effect presets.",
        inline=False,
    )
    embed.add_field(
        name="Supported formats",
        value="mp4, mov, avi, mkv, webm, gif, png, jpg, jpeg, webp",
        inline=False,
    )
    embed.add_field(
        name="Max file size",
        value="25 MB",
        inline=False,
    )
    embed.set_footer(text="I Hate The X — FFmpeg logo destruction bot")
    await ctx.reply(embed=embed)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply(f"Missing argument: `{error.param.name}`. Use `!ihtxhelp` for usage.")
        return
    raise error


if __name__ == "__main__":
    bot.run(TOKEN)
