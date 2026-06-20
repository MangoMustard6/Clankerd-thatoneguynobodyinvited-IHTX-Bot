import { Message } from 'discord.js';
import fs from 'fs';
import path from 'path';
import { spawnAsync } from '../utils/spawn.js';
import { getMaxRepetitions, getUploadLimitBytes, formatBytes } from '../utils/limits.js';
import { makeTempDir, cleanupDir, downloadUrl } from '../utils/temp.js';
import { VIDEO_EXTENSIONS, LIMITS, PROCESS_TIMEOUTS } from '../config.js';

const USAGE = [
  '**Usage:** `t!multipitchihtx [options]` — attach a video file',
  '',
  '**Options (pick one pitch mode):**',
  '`pitches=0.1|0.2|-0.3` — explicit semitone offsets, pipe-separated (sets layer count automatically)',
  '`repetitions=<n>` — auto-generate N evenly spaced pitch layers (default: 20)',
  '`spread=<n>` — total semitone range for auto mode, 0.01–999 (default: 0.4)',
  '',
  '**Time stretch:**',
  '`duration=<seconds>` — stretch/compress output to this length in seconds (optional)',
  '',
  '**Engine & window:**',
  '`engine=<r2|r3|r4>` — Rubber Band engine (default: r3)',
  '`window=<long|short>` — window mode (default: long)',
  '',
  '**Examples:**',
  '`t!multipitchihtx pitches=0|-0.1|0.1|-0.2|0.2`',
  '`t!multipitchihtx repetitions=10 spread=1.0 duration=30 engine=r2`',
].join('\n');

type Engine = 'r2' | 'r3' | 'r4';
type WindowMode = 'long' | 'short';

interface Opts {
  pitches: number[] | null;
  duration: number | null;
  repetitions: number;
  spread: number;
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
  const opts: Opts = { pitches: null, duration: null, repetitions: 20, spread: 0.4, engine: 'r3', window: 'long' };

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
      case 'length':
        return `❌ \`length\` is no longer a valid option (it was ambiguous). Use \`spread\` to set pitch range, or \`pitches\` for explicit values.`;
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

export async function handleMultipitchIHTX(
  message: Message,
  args: string[],
  ownerId: string,
): Promise<void> {
  const attachment = message.attachments.first();

  if (!attachment) {
    await message.reply(`❌ No video attachment found.\n${USAGE}`);
    return;
  }

  const ext = (attachment.name?.split('.').pop() ?? '').toLowerCase();
  if (!VIDEO_EXTENSIONS.has(ext)) {
    const supported = [...VIDEO_EXTENSIONS].join(', ');
    await message.reply(
      `❌ Unsupported file type \`.${ext}\`. Supported video formats: \`${supported}\`.`,
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
    : `${layerCount} layers, spread ${opts.spread} semitones`;
  const durationDesc = opts.duration !== null ? `, duration: ${opts.duration}s` : '';

  const status = await message.reply(
    `⏳ Processing **${layerCount}** pitch layer${layerCount !== 1 ? 's' : ''}… (engine: ${opts.engine}, window: ${opts.window}, ${modeDesc}${durationDesc})`,
  );

  const tmpDir = makeTempDir('multi');

  try {
    const inputVideo = path.join(tmpDir, `input.${ext}`);
    await status.edit(`⏳ Downloading attachment…`);
    await downloadUrl(attachment.url, inputVideo);

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

    const outputPath = path.join(tmpDir, 'output.wav');
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
      outputPath,
    );

    const mixResult = await spawnAsync('ffmpeg', ffmpegMixArgs, { timeout: PROCESS_TIMEOUTS.FFMPEG_MS });

    if (mixResult.code !== 0) {
      await status.edit(
        `❌ ffmpeg mixing failed.\n\`\`\`\n${mixResult.stderr.slice(-500)}\n\`\`\``,
      );
      return;
    }

    if (!fs.existsSync(outputPath)) {
      await status.edit('❌ Output file was not created.');
      return;
    }

    const outputSize = fs.statSync(outputPath).size;
    const uploadLimit = getUploadLimitBytes(message.guild ?? null);

    if (outputSize > uploadLimit) {
      await status.edit(
        `❌ Output exceeds Discord upload limit — ${formatBytes(outputSize)} > ${formatBytes(uploadLimit)}.`,
      );
      return;
    }

    const durSuffix = opts.duration !== null ? `, ${opts.duration}s` : '';
    const summary = opts.pitches !== null
      ? `pitches: ${offsets.map((p) => (p >= 0 ? '+' : '') + p.toFixed(3)).join(', ')}${durSuffix}`
      : `${layerCount} layers, ${opts.spread} semitone spread${durSuffix}`;

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
