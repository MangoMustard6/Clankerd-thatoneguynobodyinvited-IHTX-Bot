import { Message } from 'discord.js';
import fs from 'fs';
import path from 'path';
import { spawnAsync } from '../utils/spawn.js';
import { getUploadLimitBytes, formatBytes } from '../utils/limits.js';
import { makeTempDir, cleanupDir, listDir } from '../utils/temp.js';
import { PROCESS_TIMEOUTS } from '../config.js';

const USAGE = '**Usage:** `t!download <url>`';

function validateUrl(raw: string): URL | null {
  try {
    const u = new URL(raw);
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
    return u;
  } catch {
    return null;
  }
}

export async function handleDownload(message: Message, args: string[]): Promise<void> {
  const rawUrl = args[0];

  if (!rawUrl) {
    await message.reply(`❌ No URL provided.\n${USAGE}`);
    return;
  }

  const url = validateUrl(rawUrl);
  if (!url) {
    await message.reply('❌ Invalid URL. Only `http://` and `https://` URLs are accepted.');
    return;
  }

  const status = await message.reply('⏳ Downloading…');
  const tmpDir = makeTempDir('dl');

  try {
    const outputTemplate = path.join(tmpDir, '%(id)s.%(ext)s');

    const result = await spawnAsync(
      'yt-dlp',
      [
        url.toString(),
        '-f', 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        '--merge-output-format', 'mp4',
        '-o', outputTemplate,
        '--no-playlist',
        '--no-part',
      ],
      { timeout: PROCESS_TIMEOUTS.DOWNLOAD_MS },
    );

    if (result.code !== 0) {
      const excerpt = result.stderr.slice(-800).trim();
      await status.edit(`❌ Download failed.\n\`\`\`\n${excerpt}\n\`\`\``);
      return;
    }

    const files = listDir(tmpDir);
    if (files.length === 0) {
      await status.edit('❌ Download completed but no output file was found.');
      return;
    }

    const filePath = files.sort((a, b) => {
      return fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs;
    })[0];

    const uploadLimit = getUploadLimitBytes(message.guild ?? null);
    const size = fs.statSync(filePath).size;

    if (size > uploadLimit) {
      await status.edit(
        `❌ File too large for Discord — ${formatBytes(size)} exceeds the ${formatBytes(uploadLimit)} limit.`,
      );
      return;
    }

    await status.edit({ content: '✅ Done!', files: [filePath] });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    if (msg.includes('timed out')) {
      await status.edit('❌ Download timed out (3 min limit).');
    } else if (msg.includes('spawn error') || msg.includes('Failed to spawn')) {
      await status.edit('❌ `yt-dlp` is not available on this system.');
    } else {
      await status.edit(`❌ Unexpected error: ${msg.slice(0, 300)}`);
    }
  } finally {
    cleanupDir(tmpDir);
  }
}
