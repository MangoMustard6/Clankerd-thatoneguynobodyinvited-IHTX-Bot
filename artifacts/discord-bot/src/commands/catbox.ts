import path from 'path';
import fs from 'fs';
import os from 'os';
import { Message, EmbedBuilder } from 'discord.js';
import { spawnAsync } from '../utils/spawn.js';
import { BOT_OWNER_ID } from '../config.js';

const CATBOX_UPLOAD_SCRIPT = path.resolve('bot/catbox_upload.py');
const MAX_FILE_BYTES = 200 * 1024 * 1024; // catbox.moe limit: 200 MB
const DOWNLOAD_TIMEOUT_MS = 5 * 60 * 1000; // 5 min
const UPLOAD_TIMEOUT_MS = 5 * 60 * 1000; // 5 min

function makeTempDir(prefix: string): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), `ihtx-${prefix}-`));
}

export async function handleCatbox(message: Message): Promise<void> {
  const ownerId = BOT_OWNER_ID;
  const isOwner = ownerId !== '' && message.author.id === ownerId;

  const attachment = message.attachments.first();
  if (!attachment) {
    await message.reply('❌ Attach a file to upload (video, image, audio, etc.).');
    return;
  }

  if (attachment.size > MAX_FILE_BYTES) {
    await message.reply(`❌ File is ${(attachment.size / 1024 / 1024).toFixed(1)} MB — catbox.moe limit is 200 MB.`);
    return;
  }

  const status = await message.reply(
    `⏳ Downloading **${attachment.name}** (${(attachment.size / 1024 / 1024).toFixed(2)} MB)…`,
  );

  const tmpDir = makeTempDir('catbox');
  const localPath = path.join(tmpDir, attachment.name);

  try {
    // Download attachment
    const res = await fetch(attachment.url);
    if (!res.ok) throw new Error(`HTTP ${res.status} downloading attachment`);
    const buf = Buffer.from(await res.arrayBuffer());
    fs.writeFileSync(localPath, buf);

    await status.edit(`⏳ Uploading **${attachment.name}** to catbox.moe…`);

    // Upload via catboxpy
    const result = await spawnAsync(
      'python3',
      [CATBOX_UPLOAD_SCRIPT, localPath],
      { timeout: UPLOAD_TIMEOUT_MS },
    );

    if (result.code !== 0) {
      const errSnip = result.stderr.slice(-500);
      await status.edit(`❌ Catbox upload failed.\n\`\`\`\n${errSnip}\n\`\`\``);
      return;
    }

    const url = result.stdout.trim();
    if (!url.startsWith('http')) {
      await status.edit(`❌ Unexpected catbox response: \`${url.slice(0, 200)}\``);
      return;
    }

    const sizeMB = (attachment.size / 1024 / 1024).toFixed(2);
    const embed = new EmbedBuilder()
      .setColor(0x2ecc71)
      .setAuthor({
        name: isOwner ? `👑 ${message.author.displayName}` : message.author.displayName,
        iconURL: message.author.displayAvatarURL(),
      })
      .setTitle('✅ Uploaded to catbox.moe')
      .setDescription(`**[${attachment.name}](${url})**`)
      .addFields(
        { name: 'URL', value: url, inline: false },
        { name: 'Size', value: `${sizeMB} MB`, inline: true },
        { name: 'Type', value: attachment.contentType ?? 'unknown', inline: true },
      )
      .setFooter({ text: 'catbox.moe — permanent direct link' })
      .setTimestamp();

    await status.edit({ content: '', embeds: [embed] });
  } finally {
    try { fs.rmSync(tmpDir, { recursive: true, force: true }); } catch { }
  }
}
