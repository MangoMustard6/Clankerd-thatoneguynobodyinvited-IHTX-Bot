/**
 * effects.ts — Reusable IHTX video/audio effect functions.
 *
 * Each function follows the `ProcessorContext` contract and can be
 * called from any command or pipe-chain handler.  Effects that need
 * FFmpeg are built on `spawnAsync` so they inherit timeout handling,
 * stdout/stderr capture, and non-blocking execution.
 */

import { spawnAsync } from './utils/spawn.js';
import { PROCESS_TIMEOUTS } from './config.js';

// ── Processor context ────────────────────────────────────────────────

export interface ProcessorContext {
  inputFile: string;
  outputFile: string;
  timeout?: number;
}

// ── Video dimension probing ──────────────────────────────────────────

async function getVideoDimensions(
  filePath: string,
): Promise<{ width: number; height: number }> {
  try {
    const result = await spawnAsync('ffprobe', [
      '-v', 'error',
      '-select_streams', 'v:0',
      '-show_entries', 'stream=width,height',
      '-of', 'default=nw=1:nk=1',
      filePath,
    ], { timeout: 10_000 });

    const parts = result.stdout.trim().split(/\s+/).map(Number);
    const width = parts[0] || 1280;
    const height = parts[1] || 720;
    return { width, height };
  } catch {
    console.error('[ffprobe] Failed to resolve dimensions, falling back to 720p');
    return { width: 1280, height: 720 };
  }
}

// ── Random Jitter ────────────────────────────────────────────────────

/**
 * Apply the **Random Jitter** pixel-displacement effect.
 *
 * Uses `geq` with sinusoidal expressions to dynamically compute
 * per-frame pixel matrices.  The formula matches the legacy
 * TypeScript reference exactly:
 *
 *   indexX = i + 67   (i defaults to 1 → 68)
 *   indexY = i + 670  (i defaults to 1 → 671)
 *   divisor = 2.6666666666666665
 *
 *   exprX = ((strength/(25/3))/divisor) * (2*mod(1000*sin(N*indexX),1)-1)
 *   exprY = (strength/divisor)          * (2*mod(1000*sin(N+1000)*indexY,1)-1)
 *
 * Filter chain:
 *   rotate=0 → format=yuv444p → geq → crop → format=yuv420p
 *
 * NOT a standalone command — integrated into the core IHTX processing
 * framework as a reusable function.
 */
export async function applyRandomJitter(
  ctx: ProcessorContext,
  strengthStr: string,
): Promise<void> {
  const strength = parseFloat(strengthStr) || 10;
  const i = 1;
  const indexX = i + 67;
  const indexY = i + 670;

  const { width, height } = await getVideoDimensions(ctx.inputFile);
  const divisor = 2.6666666666666665;

  const exprX = `((${strength}/(25/3))/${divisor})*(2*mod(1000*sin(N*${indexX}),1)-1)`;
  const exprY = `(${strength}/${divisor})*(2*mod(1000*sin(N+1000)*${indexY},1)-1)`;

  const filterChain =
    `rotate=0:iw*1.1:ih*1.1,format=yuv444p,` +
    `geq='p(X+${exprX},Y+${exprY})',` +
    `crop=${width}:${height},format=yuv420p`;

  await spawnAsync('ffmpeg', [
    '-y',
    '-i', ctx.inputFile,
    '-vf', filterChain,
    ctx.outputFile,
  ], { timeout: ctx.timeout || PROCESS_TIMEOUTS.FFMPEG_MS });
}

// ── Real G-Major 4 core filter ───────────────────────────────────────

/**
 * Build the FFmpeg filter-complex string for the Real G-Major 4 effect.
 *
 * Pipeline:
 *   1. Invert all RGB channels  (curves r/g/b = 0/1 1/0)
 *   2. Split into two branches — base (inverted) & overlay (inverted + rubberband +5 st)
 *   3. Overlay pitch-shifted inverted copy on top of inverted base
 *   4. Mix both audio branches (original + pitch-shifted) with doubled volume
 *
 * This is the production TypeScript equivalent of the legacy
 * `_run_realmajor4` from `bot/ihtx_bot.py` and the dual-input
 * `realGMajor4Command` macro from the specification.
 *
 * @param inputPath  Path to the downloaded input video
 * @param outputPath Path to write the output video
 * @param timeout    Optional per-process timeout (ms)
 */
export async function applyRealGMajor4(
  inputPath: string,
  outputPath: string,
  timeout?: number,
): Promise<void> {
  // Pitch ratio for +5 semitones: 2^(5/12)
  const pitchRatio = 2 ** (5 / 12);

  const { width, height } = await getVideoDimensions(inputPath);
  if (width === 0 || height === 0) {
    throw new Error('Could not read input video dimensions.');
  }

  // Complex filter graph:
  // [0] = original input
  // Split into two branches:
  //   Branch A: RGB invert → [base] (video) + [aud0] (audio)
  //   Branch B: RGB invert + rubberband pitch +5st → [over] (video) + [aud1] (audio)
  // Overlay [over] on [base] → [vout]
  // Mix [aud0] + [aud1] with volume 2 → [aout]
  const fc = [
    // Video: split source into two branches
    `[0:v]split=2[va][vb];`,
    // Branch A: RGB invert (curves)
    `[va]curves=r='0/1 1/0':g='0/1 1/0':b='0/1 1/0',format=yuv420p[base];`,
    // Branch B: RGB invert (same curves)
    `[vb]curves=r='0/1 1/0':g='0/1 1/0':b='0/1 1/0',format=yuv420p[over];`,
    // Overlay: pitch-shifted inverted copy on top of inverted base
    `[base][over]overlay=0:0:format=auto[vout];`,
    // Audio: split, pitch-shift one branch, mix both, double volume
    `[0:a]asplit=2[aud0][aud1];`,
    `[aud1]rubberband=pitch=${pitchRatio.toFixed(6)}:window=short:transients=mixed:detector=soft:channels=together:pitchq=consistency[pitched];`,
    `[aud0][pitched]amix=inputs=2:duration=first:dropout_transition=0,volume=2[aout]`,
  ].join('');

  await spawnAsync('ffmpeg', [
    '-y',
    '-i', inputPath,
    '-filter_complex', fc,
    '-map', '[vout]',
    '-map', '[aout]',
    '-c:v', 'libx264',
    '-preset', 'fast',
    '-crf', '23',
    '-pix_fmt', 'yuv420p',
    '-c:a', 'aac',
    '-b:a', '128k',
    '-movflags', '+faststart',
    outputPath,
  ], { timeout: timeout || PROCESS_TIMEOUTS.REALGM4_MS });
}
