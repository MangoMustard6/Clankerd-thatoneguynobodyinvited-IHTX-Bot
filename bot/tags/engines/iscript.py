"""
iscript: engine — ImageMagick-powered image processing.

Usage inside a tag:
    {iscript:
        load_attachment
        blur 5
        grayscale
        output png
    }

Available operations:
    load_attachment          — use the first attachment from the invoking message
    load URL                 — download and load an image from a URL
    blur RADIUS              — Gaussian blur (0–50)
    sharpen AMOUNT           — unsharp mask sharpening (0–50)
    grayscale                — convert to grayscale
    negate                   — invert colours
    pixelate SIZE            — pixelate (1–100)
    sepia                    — sepia tone
    brightness N             — brightness adjustment (-100 to 100)
    contrast N               — contrast adjustment (-100 to 100)
    rotate DEGREES           — rotate image
    resize WxH               — resize to exact dimensions (e.g. 640x480)
    thumbnail WxH            — resize, keeping aspect ratio
    flip                     — flip vertically
    flop                     — flip horizontally
    output FORMAT            — jpg | png | gif | webp  (default: png)
"""

import asyncio
import io
import os
import re
import shutil
import tempfile
from pathlib import Path

import aiohttp
import discord

from . import BaseEngine, EngineResult

_ALLOWED_OPS = frozenset({
    "load_attachment", "load", "blur", "sharpen", "grayscale", "negate",
    "pixelate", "sepia", "brightness", "contrast", "rotate", "resize",
    "thumbnail", "flip", "flop", "output",
})
_ALLOWED_FORMATS = frozenset({"jpg", "jpeg", "png", "gif", "webp"})
_MAX_BYTES = 8 * 1024 * 1024


class IScriptEngine(BaseEngine):
    name = "iscript"

    async def execute(self, content: str, ctx, tag_ctx: dict) -> EngineResult:
        lines = [
            ln.strip()
            for ln in content.strip().splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        if not lines:
            return EngineResult(error="iscript: no commands")

        tmpdir = tempfile.mkdtemp(prefix="iscript_")
        try:
            return await self._run(lines, ctx, tmpdir)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def _run(self, lines: list, ctx, tmpdir: str) -> EngineResult:
        input_path: str | None = None
        output_format = "png"
        convert_args: list = []

        for line in lines:
            parts = line.split(None, 1)
            op = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""

            if op not in _ALLOWED_OPS:
                return EngineResult(error=f"iscript: unknown operation '{op}'")

            if op == "load_attachment":
                if not ctx.message.attachments:
                    return EngineResult(error="iscript: no attachment in message")
                att = ctx.message.attachments[0]
                suffix = Path(att.filename).suffix or ".png"
                input_path = os.path.join(tmpdir, "input" + suffix)
                await att.save(input_path)

            elif op == "load":
                if not arg.startswith(("http://", "https://")):
                    return EngineResult(error="iscript: load only accepts http/https URLs")
                input_path = os.path.join(tmpdir, "input.png")
                try:
                    async with aiohttp.ClientSession() as s:
                        async with s.get(arg, timeout=aiohttp.ClientTimeout(total=15)) as r:
                            if r.status != 200:
                                return EngineResult(error=f"iscript: HTTP {r.status}")
                            data = await r.read()
                            Path(input_path).write_bytes(data)
                except Exception as exc:
                    return EngineResult(error=f"iscript: load failed — {exc}")

            elif op == "blur":
                try:
                    radius = max(0.0, min(float(arg), 50.0))
                    convert_args += ["-blur", f"0x{radius}"]
                except ValueError:
                    return EngineResult(error="iscript: blur requires a number")

            elif op == "sharpen":
                try:
                    amt = max(0.0, min(float(arg), 50.0))
                    convert_args += ["-sharpen", f"0x{amt}"]
                except ValueError:
                    return EngineResult(error="iscript: sharpen requires a number")

            elif op == "grayscale":
                convert_args += ["-colorspace", "Gray"]

            elif op == "negate":
                convert_args += ["-negate"]

            elif op == "pixelate":
                try:
                    size = max(1, min(int(arg), 100))
                    convert_args += [
                        "-scale", f"{size}%",
                        "-scale", "10000%",
                    ]
                except ValueError:
                    return EngineResult(error="iscript: pixelate requires an integer")

            elif op == "sepia":
                convert_args += ["-sepia-tone", "80%"]

            elif op == "brightness":
                try:
                    val = max(-100, min(int(arg), 100))
                    convert_args += ["-brightness-contrast", f"{val}x0"]
                except ValueError:
                    return EngineResult(error="iscript: brightness requires an integer (-100–100)")

            elif op == "contrast":
                try:
                    val = max(-100, min(int(arg), 100))
                    convert_args += ["-brightness-contrast", f"0x{val}"]
                except ValueError:
                    return EngineResult(error="iscript: contrast requires an integer (-100–100)")

            elif op == "rotate":
                try:
                    deg = float(arg) % 360
                    convert_args += ["-rotate", str(deg)]
                except ValueError:
                    return EngineResult(error="iscript: rotate requires a number")

            elif op == "resize":
                if not re.fullmatch(r"\d+x\d+", arg):
                    return EngineResult(error="iscript: resize format is WxH (e.g. 640x480)")
                convert_args += ["-resize", arg + "!"]

            elif op == "thumbnail":
                if not re.fullmatch(r"\d+x\d+", arg):
                    return EngineResult(error="iscript: thumbnail format is WxH")
                convert_args += ["-thumbnail", arg]

            elif op == "flip":
                convert_args += ["-flip"]

            elif op == "flop":
                convert_args += ["-flop"]

            elif op == "output":
                fmt = arg.lower().lstrip(".")
                if fmt not in _ALLOWED_FORMATS:
                    return EngineResult(
                        error="iscript: output must be jpg/png/gif/webp"
                    )
                output_format = "jpg" if fmt == "jpeg" else fmt

        if input_path is None:
            return EngineResult(
                error="iscript: no input — use 'load_attachment' or 'load URL'"
            )

        output_path = os.path.join(tmpdir, f"output.{output_format}")
        cmd = ["convert", input_path] + convert_args + [output_path]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        except asyncio.TimeoutError:
            return EngineResult(error="iscript: timed out (30 s limit)")
        except FileNotFoundError:
            return EngineResult(error="iscript: ImageMagick (convert) not available")

        if proc.returncode != 0:
            msg = stderr.decode(errors="replace")[:200].strip()
            return EngineResult(error=f"iscript: ImageMagick error — {msg}")

        out_bytes = Path(output_path).read_bytes()
        if len(out_bytes) > _MAX_BYTES:
            return EngineResult(error="iscript: output too large for Discord (max 8 MB)")

        return EngineResult(
            files=[discord.File(io.BytesIO(out_bytes), filename=f"iscript.{output_format}")]
        )
