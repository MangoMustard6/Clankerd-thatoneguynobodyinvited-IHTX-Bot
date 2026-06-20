import { Message } from 'discord.js';
import fs from 'fs';
import path from 'path';
import { spawnAsync } from '../utils/spawn.js';
import { getMaxRepetitions, getUploadLimitBytes, formatBytes } from '../utils/limits.js';
import { makeTempDir, cleanupDir, downloadUrl } from '../utils/temp.js';
import { VIDEO_EXTENSIONS, LIMITS, PROCESS_TIMEOUTS } from '../config.js';

const USAGE = [
  '**Usage:** `t!multipitchihtx [options]` — attach a video/audio file',
  '',
  '**Pitch mode (pick one):**',
  '`pitches=0.1|0.2|-0.3` — explicit semitone offsets, pipe-separated',
  '`repetitions=<n>` — auto N evenly spaced pitch layers (default: 20)',
  '`spread=<n>` — total semitone range for auto mode (default: 0.4)',
  '',
  '**Concatenation:**',
  '`concat=<n>` — repeat the entire mixed output N times end-to-end (default: 1)',
  '',
  '**Time stretch:**',
  '`duration=<seconds>` — stretch each layer to this length via rubberband (optional)',
  '',
  '**Engine:**',
  '`engine=<r2|r3|r4>` — Rubber Band engine (default: r3)',
  '`window=<long|short>` — window mode (default: long)',
  '',
  '**Examples:**',
  '`t!multipitchihtx pitches=0|-0.1|0.1|-0.2|0.2`',
  '`t!multipitchihtx repetitions=10 spread=1.0 concat=3`',
  '`t!multipitchihtx concat=5 duration=8 spread=0.8 engine=r2`',
].join('\n');

type Engine = 'r2' | 'r3' | 'r4';
type WindowMode = 'long' | 'short';

interface Opts {
  pitches: number[] | null;
  duration: number | null;
  repetitions: number;
  spread: number;
  concat: number;
  engine: Engine;
  window: WindowMode;
}

const ENGINE_FLAGS: Record<Engine, string> = {
  r2: '-2',
  r3: '-3',
  r4: '-3',
};

function windowFlags(engine: Engine, window: WindowMode): string[] {
  if (window === 'short') return ['--window-short'];
  if (window === 'long' && engine === 'r2') return ['--window-long'];
  return [];
}

function parseArgs(args: string[]): Opts | string {
  const opts: Opts = {
    pitches: null,
    duration: null,
    repetitions: 20,
    spread: 0.4,
    concat: 1,
    engine: 'r3',
    window: 'long',
  };

  for (const arg of args) {
    const eqIdx = arg.indexOf('=');
    if (eqIdx === -1) continue;
    const key = arg.slice(0, eqIdx).toLowerCase().trim();
    const val = arg.slice(eqIdx + 1).trim();

    switch (key) {
      case 'pitches': {
        const parts = val.split('|').map((p) => Number(p.trim()));
        if (parts.length === 0) return `❌ \`pitches\` must have at least one value.`;
        if (parts.some(isNaN)) return `❌ \`pitches\` contains a non-numeric value.`;
        if (parts.some((p) => Math.abs(p) > 9999)) return `❌ Pitch offsets must be between -9999 and 9999 semitones.`;
        opts.pitches = parts;
        break;
      }
      case 'duration': {
        const n = Number(val);
        if (isNaN(n) || n <= 0 || n > 86400) return `❌ \`duration\` must be a positive number of seconds (max 86400).`;
        opts.duration = n;
        break;
      }
      case 'repetitions': {
        const n = Number(val);
        if (!Number.isInteger(n) || n < 1) return `❌ \`repetitions\` must be an integer ≥ 1.`;
        opts.repetitions = n;
        break;
      }
      case 'spread': {
        const n = Number(val);
        if (isNaN(n) || n < 0.01 || n > 999) return `❌ \`spread\` must be between 0.01 and 999.`;
        opts.spread = n;
        break;
      }
      case 'concat': {
        const n = Number(val);
        if (!Number.isInteger(n) || n < 1 || n > 1000) return `❌ \`concat\` must be an integer between 1 and 1000.`;
        opts.concat = n;
        break;
      }
      case 'length':
        return `❌ \`length\` is no longer valid. Use \`spread\` for pitch range, \`pitches\` for explicit values, or \`concat\` to repeat the output.`;
      case 'engine': {
        if (!['r2', 'r3', 'r4'].includes(val.toLowerCase())) {
          return `❌ \`engine\` must be one of: \`r2\`, \`r3\`, \`r4\`.`;
        }
        opts.engine = val.toLowerCase() as Engine;
        break;
      }
      case 'window': {
        if (!['long', 'short'].includes(val.toLowerCase())) {
          return `❌ \`window\` must be \`long\` or \`short\`.`;
        }
        opts.window = val.toLowerCase() as WindowMode;
        break;
      }
      default:
        return `❌ Unknown option \`${key}\`.\n${USAGE}`;
    }
  }

  return opts;
}

function linspace(from: number, to: number, n: number): number[] {
  if (n === 1) return [0];
  const step = (to - from) / (n - 1);
  return Array.from({ length: n }, (_, i) => from + step * i);
}

function resolvePitchOffsets(opts: Opts): number[] {
  if (opts.pitches !== null) return opts.pitches;
  const half = opts.spread / 2;
  return linspace(-half, half, opts.repetitions);
}

async function resolveAttachment(message: Message): Promise<{ url: string; name: string; ext: string } | null> {
  // 1. Direct attachment
  const direct = message.attachments.first();
  if (direct) {
    const ext = (direct.name?.split('.').pop() ?? '').toLowerCase();
    return { url: direct.url, name: direct.name, ext };
  }

  // 2. Replied-to message attachment
  if (message.reference?.messageId) {
    try {
      const ref = await message.fetchReference();
      const refAttachment = ref.attachments.first();
      if (refAttachment) {
        const ext = (refAttachment.name?.split('.').pop() ?? '').toLowerCase();
        return { url: refAttachment.url, name: refAttachment.name, ext };
      }
    } catch { }
  }

  return null;
}

export async function handleMultipitchIHTX(
  message: Message,
  args: string[],
  ownerId: string,
): Promise<void> {
  const attachmentInfo = await resolveAttachment(message);

  if (!attachmentInfo) {
    await message.reply(`❌ No video/audio attachment found. Attach a file or reply to a message with one.\n${USAGE}`);
    return;
  }

  const { url: attachmentUrl, name: attachmentName, ext } = attachmentInfo;

  if (!VIDEO_EXTENSIONS.has(ext)) {
    const supported = [...VIDEO_EXTENSIONS].join(', ');
    await message.reply(
      `❌ Unsupported file type \`.${ext}\`. Supported: \`${supported}\`.`,
    );
    return;
  }

  const parsed = parseArgs(args);
  if (typeof parsed === 'string') {
    await message.reply(parsed);
    return;
  }

  const opts = parsed;
  const maxLayers = getMaxRepetitions(message.author.id, ownerId, message.guild ?? null);
  const offsets = resolvePitchOffsets(opts);
  const layerCount = offsets.length;

  if (layerCount > maxLayers) {
    const isOwner = ownerId !== '' && message.author.id === ownerId;
    const base = isOwner ? LIMITS.OWNER_MAX_REPS : LIMITS.NON_OWNER_MAX_REPS;
    const boosted = (message.guild?.premiumTier ?? 0) >= 1 ? ` (+${LIMITS.BOOST_BONUS} boost)` : '';
    await message.reply(
      `❌ **${layerCount}** pitch layers exceeds your limit of **${maxLayers}** (base ${base}${boosted}).`,
    );
    return;
  }

  if (layerCount < LIMITS.MIN_REPS) {
    await message.reply(`❌ Must have at least ${LIMITS.MIN_REPS} pitch layer.`);
    return;
  }

  const modeDesc = opts.pitches !== null
    ? `pitches: ${offsets.map((p) => p.toFixed(3)).join(', ')}`
    : `${layerCount} layers, spread ${opts.spread} st`;
  const concatDesc = opts.concat > 1 ? `, concat ×${opts.concat}` : '';
  const durationDesc = opts.duration !== null ? `, duration: ${opts.duration}s` : '';

  const status = await message.reply(
    `⏳ Processing **${layerCount}** pitch layer${layerCount !== 1 ? 's' : ''}… (engine: ${opts.engine}, window: ${opts.window}, ${modeDesc}${durationDesc}${concatDesc})`,
  );

  const tmpDir = makeTempDir('multi');

  try {
    const inputVideo = path.join(tmpDir, `input.${ext}`);
    await status.edit(`⏳ Downloading attachment…`);
    await downloadUrl(attachmentUrl, inputVideo);

    const inputAudio = path.join(tmpDir, 'input.wav');
    await status.edit(`⏳ Extracting audio…`);

    const extractResult = await spawnAsync(
      'ffmpeg',
      ['-y', '-i', inputVideo, '-vn', '-acodec', 'pcm_s16le', '-ar', '44100', '-ac', '2', inputAudio],
      { timeout: PROCESS_TIMEOUTS.FFMPEG_MS },
    );

    if (extractResult.code !== 0) {
      await status.edit(
        `❌ ffmpeg failed to extract audio.\n\`\`\`\n${extractResult.stderr.slice(-500)}\n\`\`\``,
      );
      return;
    }

    const layerPaths: string[] = [];
    const engineFlag = ENGINE_FLAGS[opts.engine];
    const winFlags = windowFlags(opts.engine, opts.window);

    for (let i = 0; i < offsets.length; i++) {
      await status.edit(`⏳ Generating pitch layer ${i + 1}/${offsets.length}…`);

      const layerPath = path.join(tmpDir, `layer_${i}.wav`);
      const pitchSemitones = offsets[i].toFixed(6);

      const rbArgs = [engineFlag, ...winFlags];
      if (opts.duration !== null) rbArgs.push('--duration', String(opts.duration));
      rbArgs.push('--pitch', pitchSemitones, inputAudio, layerPath);

      const rbResult = await spawnAsync('rubberband', rbArgs, { timeout: PROCESS_TIMEOUTS.RUBBERBAND_MS });

      if (rbResult.code !== 0) {
        await status.edit(
          `❌ rubberband failed on layer ${i + 1}.\n\`\`\`\n${rbResult.stderr.slice(-400)}\n\`\`\``,
        );
        return;
      }

      layerPaths.push(layerPath);
    }

    await status.edit(`⏳ Mixing ${layerPaths.length} layers…`);

    const mixedPath = path.join(tmpDir, 'mixed.wav');
    const ffmpegMixArgs: string[] = ['-y'];
    for (const lp of layerPaths) ffmpegMixArgs.push('-i', lp);

    const filterGraph =
      layerPaths.length === 1
        ? 'alimiter=limit=0.99:level=false'
        : `amix=inputs=${layerPaths.length}:duration=longest:normalize=0,alimiter=limit=0.99:level=false`;

    ffmpegMixArgs.push(
      '-filter_complex', filterGraph,
      '-acodec', 'pcm_s16le',
      '-ar', '44100',
      '-ac', '2',
      mixedPath,
    );

    const mixResult = await spawnAsync('ffmpeg', ffmpegMixArgs, { timeout: PROCESS_TIMEOUTS.FFMPEG_MS });

    if (mixResult.code !== 0) {
      await status.edit(
        `❌ ffmpeg mixing failed.\n\`\`\`\n${mixResult.stderr.slice(-500)}\n\`\`\``,
      );
      return;
    }

    // ── Concatenation ─────────────────────────────────────────────────────────
    let outputPath = mixedPath;

    if (opts.concat > 1) {
      await status.edit(`⏳ Concatenating ×${opts.concat}…`);
      outputPath = path.join(tmpDir, 'output.wav');

      const concatResult = await spawnAsync(
        'ffmpeg',
        ['-y', '-stream_loop', String(opts.concat - 1), '-i', mixedPath, '-c', 'copy', outputPath],
        { timeout: PROCESS_TIMEOUTS.FFMPEG_MS },
      );

      if (concatResult.code !== 0) {
        await status.edit(
          `❌ ffmpeg concat failed.\n\`\`\`\n${concatResult.stderr.slice(-500)}\n\`\`\``,
        );
        return;
      }
    }

    if (!fs.existsSync(outputPath)) {
      await status.edit('❌ Output file was not created.');
      return;
    }

    const outputSize = fs.statSync(outputPath).size;
    const uploadLimit = getUploadLimitBytes(message.guild ?? null);

    if (outputSize > uploadLimit) {
      await status.edit(
        `❌ Output (${formatBytes(outputSize)}) exceeds Discord upload limit (${formatBytes(uploadLimit)}).`,
      );
      return;
    }

    const durSuffix = opts.duration !== null ? `, ${opts.duration}s/layer` : '';
    const concatSuffix = opts.concat > 1 ? ` ×${opts.concat}` : '';
    const summary = opts.pitches !== null
      ? `pitches: ${offsets.map((p) => (p >= 0 ? '+' : '') + p.toFixed(3)).join(', ')}${durSuffix}${concatSuffix}`
      : `${layerCount} layers, ${opts.spread} st spread${durSuffix}${concatSuffix}`;

    await status.edit({
      content: `✅ Done! (${summary})`,
      files: [{ attachment: outputPath, name: 'multipitchihtx.wav' }],
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    await status.edit(msg.includes('timed out')
      ? '❌ Processing timed out.'
      : `❌ Error: ${msg.slice(0, 300)}`);
  } finally {
    cleanupDir(tmpDir);
  }
}
