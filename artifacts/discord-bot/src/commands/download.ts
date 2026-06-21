import { Message } from 'discord.js';
import fs from 'fs';
import path from 'path';
import { spawnAsync } from '../utils/spawn.js';
import { getUploadLimitBytes, formatBytes } from '../utils/limits.js';
import { makeTempDir, cleanupDir, listDir } from '../utils/temp.js';
import { PROCESS_TIMEOUTS } from '../config.js';
import { _upload_to_catbox } from '../utils/catbox.js';

const USAGE = '**Usage:** `t!download <url>`  *(aliases: dl, dv, dlv)*';

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
    await message.reply('idk bro 😭');
    return;
  }

  const username = message.member?.displayName ?? message.author.username;
  const status = await message.reply(
    `son im crine... fetching this clip for ${username.toLowerCase()} 🥀\ndownloading parameters rn 🫩`,
  );

  const tmpDir = makeTempDir('dl');

  try {
    const outputTemplate = path.join(tmpDir, '%(id)s.%(ext)s');

    const result = await spawnAsync(
      'yt-dlp',
      [
        url.toString(),
        '-f', 'bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best',
        '--merge-output-format', 'mp4',
        '-o', outputTemplate,
        '--no-playlist',
        '--no-part',
      ],
      { timeout: PROCESS_TIMEOUTS.DOWNLOAD_MS },
    );

    if (result.code !== 0) {
      const excerpt = result.stderr.slice(-800).trim();
      await status.edit(`idk bro 😭\n\`\`\`\n${excerpt}\n\`\`\``);
      return;
    }

    const files = listDir(tmpDir);
    if (files.length === 0) {
      await status.edit('idk bro 😭');
      return;
    }

    const filePath = files.sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs)[0];
    const uploadLimit = getUploadLimitBytes(message.guild ?? null);
    const size = fs.statSync(filePath).size;

    if (size > uploadLimit) {
      await status.edit(`file too heavy tbh... uploading to catbox instead 🥀`);
      const catboxUrl = await _upload_to_catbox(filePath);
      if (catboxUrl) {
        await status.edit(
          `here ✌️ (too heavy for discord so catbox it is)\n${catboxUrl}\n-# ${formatBytes(size)}`,
        );
      } else {
        await status.edit(`file too heavy tbh... cant upload 💀\n-# ${formatBytes(size)} — too big for Discord and Catbox upload failed`);
      }
      return;
    }

    await status.edit({ content: `here ✌️`, files: [filePath] });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    if (msg.includes('timed out')) {
      await status.edit('idk bro 😭 (timed out)');
    } else if (msg.includes('spawn error') || msg.includes('Failed to spawn')) {
      await status.edit('idk bro 😭 (`yt-dlp` not found)');
    } else {
      await status.edit(`idk bro 😭\n\`\`\`\n${msg.slice(0, 300)}\n\`\`\``);
    }
  } finally {
    cleanupDir(tmpDir);
  }
}
