# IHTX Bot — I Hate The X FFmpeg Discord Bot

A Discord bot that applies destructive FFmpeg visual effects to videos and images. Upload a file, pick a preset, and get back a mangled masterpiece.

## Quick Start

### Prerequisites

- Python 3.11+
- [FFmpeg](https://ffmpeg.org/) (required)
- [sox](http://sox.sourceforge.net/) (optional, for audio effects)

### Install

```bash
pip install -r requirements.txt
```

### Run

Set your Discord bot token as an environment variable, then start the bot:

```bash
export DISCORD_TOKEN="your-bot-token-here"
python3 bot/ihtx_bot.py
```

Or use the entry point:

```bash
python3 main.py
```

## Commands

| Command | Aliases | Description |
|---------|---------|-------------|
| `g!ihtx [preset]` | `g!effect`, `g!destroy` | Apply an FFmpeg effect to an attached video/image |
| `g!presets` | `g!effects`, `g!list` | List all available effect presets |
| `g!ihtxhelp` | `g!bothelp` | Show help embed |

### Owner-Only Commands

| Command | Description |
|---------|-------------|
| `g!blockuser <id\|mention>` | Block a user from using the bot |
| `g!unblockuser <id\|mention>` | Unblock a user |
| `g!blockchannel [id\|mention]` | Block a channel (current channel if omitted) |
| `g!unblockchannel [id\|mention]` | Unblock a channel |
| `g!say <message>` | Send a plain message as the bot |
| `g!sayembed <title> \| <description>` | Send an embed as the bot |

## Presets

| Preset | Description |
|--------|-------------|
| `chaos` | Shake + noise + hue rotation + high contrast (default) |
| `glitch` | RGB shift + noise + high contrast grayscale |
| `shake` | Shake + noise + boosted contrast/saturation |
| `rainbow` | RGB channel split and additive blend |
| `static` | Noise + vintage curve + mild contrast |
| `melt` | Perspective warp + noise |
| `corrupt` | Grid overlay + noise + high gamma/contrast |

## Configuration

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DISCORD_TOKEN` | Yes | Discord bot token |

### Data Files (auto-created in `bot/`)

| File | Purpose |
|------|---------|
| `owner_ids.json` | List of owner user IDs (full access) |
| `limits.json` | Per-user heavy command limits |
| `blocklist.json` | Blocked user IDs |
| `channel_blocks.json` | Blocked channel IDs |
| `tags.json` | Custom tag/preset definitions |
| `autoreply.json` | Auto-reply configuration |

### Constants (edit in source)

- `MAX_FILE_SIZE` — 25 MB (Discord upload limit)
- `MAX_DURATION` — 600 seconds
- `MAX_REPETITIONS` — 100
- `HEAVY_LIMIT_DEFAULT` — 10 heavy commands per 24h for non-owners
- Command prefix: `g!`

## Docker

```bash
docker build -t ihtx-bot .
docker run -e DISCORD_TOKEN="your-token" ihtx-bot
```

## Project Structure

```
├── bot/
│   ├── ihtx_bot.py        # Main Discord bot
│   ├── displacemaps/      # FFmpeg displacement assets
│   ├── owner_ids.json
│   ├── limits.json
│   ├── tags.json
│   ├── autoreply.json
│   ├── blocklist.json
│   └── channel_blocks.json
├── main.py                # Entry point
├── requirements.txt       # Python dependencies
├── Dockerfile             # Container build
├── .replit                # Replit configuration
├── replit.nix             # Nix system deps (ffmpeg, sox)
├── artifacts/             # TypeScript API server & mockup sandbox
├── lib/                   # Shared TypeScript libraries (API spec, DB, Zod)
└── scripts/               # Build & maintenance scripts
```

## License

Private project — all rights reserved.
