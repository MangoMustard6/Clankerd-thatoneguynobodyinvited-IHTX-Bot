import { Message } from 'discord.js';
import fs from 'fs';
import path from 'path';
import { spawnAsync } from '../utils/spawn.js';
import { getMaxRepetitions, getUploadLimitBytes, formatBytes } from '../utils/limits.js';
import { makeTempDir, cleanupDir, downloadUrl } from '../utils/temp.js';
import { VIDEO_EXTENSIONS, BOT_OWNER_ID, LIMITS, PROCESS_TIMEOUTS } from '../config.js';

const USAGE = [
  '**Usage:** `t!multipitchihtx [options]` — attach a video file',
  '',
  '**Options:**',
  '`repetitions=<1–30>` — number of pitch layers (default: 20)',
  '`length=<0.01–999>` — total pitch spread in semitones (default: 0.4)',
  '`engine=<r2|r3|r4>` — Rubber Band engine (default: r3)',
  '`window=<long|short>` — window mode (default: long)',
].join('\n');

type Engine = 'r2' | 'r3' | 'r4';
type WindowMode = 'long' | 'short';

interface Opts {
  repetitions: number;
  length: number;
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
  const opts: Opts = { repetitions: 20, length: 0.4, engine: 'r3', window: 'long' };

  for (const arg of args) {
    const [key, val] = arg.split('=');
    if (!key || val === undefined) continue;

    switch (key.toLowerCase()) {
      case 'repetitions': {
        const n = Number(val);
        if (!Number.isInteger(n) || n < 1) return `❌ \`repetitions\` must be an integer ≥ 1.`;
        opts.repetitions = n;
        break;
      }
      case 'length': {
        const n = Number(val);
        if (isNaN(n) || n < 0.01 || n > 999) return `❌ \`length\` must be between 0.01 and 999.`;
        opts.length = n;
        break;
      }
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
        return `❌ Unknown option \`${key}\`. ${USAGE}`;
    }
  }

  return opts;
}

function linspace(from: number, to: number, n: number): number[] {
  if (n === 1) return [0];
  const step = (to - from) / (n - 1);
  return Array.from({ length: n }, (_, i) => from + step * i);
}

function pitchOffsets(repetitions: number, length: number): number[] {
  const half = length / 2;
  return linspace(-half, half, repetitions);
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
  const maxReps = getMaxRepetitions(message.author.id, ownerId, message.guild ?? null);

  if (opts.repetitions > maxReps) {
    const isOwner = ownerId !== '' && message.author.id === ownerId;
    const base = isOwner ? LIMITS.OWNER_MAX_REPS : LIMITS.NON_OWNER_MAX_REPS;
    const boosted = message.guild?.premiumTier ? ` (+${LIMITS.BOOST_BONUS} boost bonus)` : '';
    await message.reply(
      `❌ \`repetitions\` exceeds your limit of **${maxReps}** (base ${base}${boosted}).`,
    );
    return;
  }

  if (opts.repetitions < LIMITS.MIN_REPS) {
    await message.reply(`❌ \`repetitions\` must be at least ${LIMITS.MIN_REPS}.`);
    return;
  }

  const status = await message.reply(
    `⏳ Processing **${opts.repetitions}** pitch layers… (engine: ${opts.engine}, window: ${opts.window}, spread: ${opts.length} semitones)`,
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
      [
        '-y',
        '-i', inputVideo,
        '-vn',
        '-acodec', 'pcm_s16le',
        '-ar', '44100',
        '-ac', '2',
        inputAudio,
      ],
      { timeout: PROCESS_TIMEOUTS.FFMPEG_MS },
    );

    if (extractResult.code !== 0) {
      await status.edit(
        `❌ ffmpeg failed to extract audio.\n\`\`\`\n${extractResult.stderr.slice(-500)}\n\`\`\``,
      );
      return;
    }

    const offsets = pitchOffsets(opts.repetitions, opts.length);
    const layerPaths: string[] = [];

    const engineFlag = ENGINE_FLAGS[opts.engine];
    const winFlags = windowFlags(opts.engine, opts.window);

    for (let i = 0; i < offsets.length; i++) {
      await status.edit(`⏳ Generating pitch layer ${i + 1}/${offsets.length}…`);

      const layerPath = path.join(tmpDir, `layer_${i}.wav`);
      const pitchSemitones = offsets[i].toFixed(6);

      const rbResult = await spawnAsync(
        'rubberband',
        [
          engineFlag,
          ...winFlags,
          '--pitch', pitchSemitones,
          inputAudio,
          layerPath,
        ],
        { timeout: PROCESS_TIMEOUTS.RUBBERBAND_MS },
      );

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

    const mixResult = await spawnAsync('ffmpeg', ffmpegMixArgs, {
      timeout: PROCESS_TIMEOUTS.FFMPEG_MS,
    });

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

    await status.edit({
      content: `✅ Done! ${opts.repetitions} pitch layers, spread ${opts.length} semitones.`,
      files: [{ attachment: outputPath, name: 'multipitchihtx.wav' }],
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    if (msg.includes('timed out')) {
      await status.edit('❌ Processing timed out.');
    } else {
      await status.edit(`❌ Error: ${msg.slice(0, 300)}`);
    }
  } finally {
    cleanupDir(tmpDir);
  }
}
