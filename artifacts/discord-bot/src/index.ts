import {
  Client,
  GatewayIntentBits,
  Partials,
  Message,
} from 'discord.js';
import { BOT_TOKEN, BOT_OWNER_ID, PREFIX } from './config.js';
import { handleDownload } from './commands/download.js';
import { handleMultipitchIHTX } from './commands/multipitchihtx.js';
import { handleHelp } from './commands/help.js';
import { handleCoinflip } from './commands/games/coinflip.js';
import { handleDice } from './commands/games/dice.js';
import { handleRPS } from './commands/games/rps.js';
import { handleEightBall } from './commands/games/eightball.js';
import { handleSlots } from './commands/games/slots.js';
import { handleChoose } from './commands/games/choose.js';
import { handleRoulette } from './commands/games/roulette.js';
import { handleTrivia } from './commands/games/trivia.js';
import { handleInfo } from './commands/info.js';
import { handleCatbox } from './commands/catbox.js';
import { handleChat } from './commands/chat.js';

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
  console.log(`[IHTX-TS] Owner ID: ${BOT_OWNER_ID || '(not set)'}`);
  console.log(`[IHTX-TS] Commands: download, multipitchihtx, chat, ask, coinflip, dice, rps, 8ball, slots, choose, roulette, trivia, help, info, catbox`);
});

client.on('messageCreate', async (message: Message) => {
  if (message.author.bot) return;
  if (!message.content.startsWith(PREFIX)) return;

  const body = message.content.slice(PREFIX.length).trimStart();
  const spaceIdx = body.search(/\s/);
  const command = (spaceIdx === -1 ? body : body.slice(0, spaceIdx)).toLowerCase();
  const rest = spaceIdx === -1 ? '' : body.slice(spaceIdx + 1);
  const args = rest.trim() ? rest.trim().split(/\s+/) : [];

  try {
    switch (command) {
      case 'download':
        await handleDownload(message, args);
        break;

      case 'multipitchihtx':
        await handleMultipitchIHTX(message, args, BOT_OWNER_ID);
        break;

      case 'help':
        await handleHelp(message, BOT_OWNER_ID);
        break;

      case 'coinflip':
      case 'cf':
        await handleCoinflip(message, BOT_OWNER_ID);
        break;

      case 'dice':
      case 'roll':
        await handleDice(message, args, BOT_OWNER_ID);
        break;

      case 'rps':
        await handleRPS(message, args, BOT_OWNER_ID);
        break;

      case '8ball':
        await handleEightBall(message, args, BOT_OWNER_ID);
        break;

      case 'slots':
      case 'slot':
        await handleSlots(message, BOT_OWNER_ID);
        break;

      case 'choose':
      case 'pick': {
        const chooseArgs = rest.trim() ? [rest.trim()] : [];
        await handleChoose(message, chooseArgs, BOT_OWNER_ID);
        break;
      }

      case 'roulette':
        await handleRoulette(message, args, BOT_OWNER_ID);
        break;

      case 'trivia':
        await handleTrivia(message, BOT_OWNER_ID);
        break;

      case 'info':
        await handleInfo(message);
        break;

      case 'chat':
      case 'ask': {
        const chatArgs = rest.trim() ? [rest.trim()] : [];
        await handleChat(message, chatArgs);
        break;
      }

      case 'catbox':
      case 'cb':
      case 'upload':
        await handleCatbox(message, args);
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
