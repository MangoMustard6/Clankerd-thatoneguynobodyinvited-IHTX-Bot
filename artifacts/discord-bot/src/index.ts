import {
  Client,
  GatewayIntentBits,
  Partials,
  Message,
} from 'discord.js';
import { BOT_TOKEN, BOT_OWNER_ID, PREFIX } from './config.js';
import { handleDownload } from './commands/download.js';
import { handleMultipitchIHTX } from './commands/multipitchihtx.js';

if (!BOT_TOKEN) {
  console.error('ERROR: DISCORD_TOKEN environment variable is not set.');
  process.exit(1);
}

const client = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildMessages,
    GatewayIntentBits.MessageContent,
    GatewayIntentBits.DirectMessages,
  ],
  partials: [Partials.Channel],
});

client.once('clientReady', (c) => {
  console.log(`[IHTX-TS] Logged in as ${c.user.tag}`);
  console.log(`[IHTX-TS] Prefix: ${PREFIX}`);
  console.log(`[IHTX-TS] Owner ID: ${BOT_OWNER_ID || '(not set — owner commands disabled)'}`);
});

client.on('messageCreate', async (message: Message) => {
  if (message.author.bot) return;
  if (!message.content.startsWith(PREFIX)) return;

  const body = message.content.slice(PREFIX.length).trimStart();
  const parts = body.split(/\s+/);
  const command = parts[0]?.toLowerCase() ?? '';
  const args = parts.slice(1);

  try {
    switch (command) {
      case 'download':
        await handleDownload(message, args);
        break;

      case 'multipitchihtx':
        await handleMultipitchIHTX(message, args, BOT_OWNER_ID);
        break;

      default:
        break;
    }
  } catch (err) {
    console.error(`[IHTX-TS] Unhandled error in command "${command}":`, err);
    try {
      await message.reply('❌ An unexpected error occurred. Please try again.');
    } catch {
    }
  }
});

client.on('error', (err) => {
  console.error('[IHTX-TS] Client error:', err);
});

client.login(BOT_TOKEN).catch((err) => {
  console.error('[IHTX-TS] Failed to log in:', err);
  process.exit(1);
});
