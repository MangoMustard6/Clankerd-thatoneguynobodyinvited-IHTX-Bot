import { Message, TextChannel, DMChannel, NewsChannel, ThreadChannel } from 'discord.js';
import { GoogleGenAI } from '@google/genai';
import { PREFIX } from '../config.js';

let _ai: GoogleGenAI | null = null;

function getAI(): GoogleGenAI | null {
  if (!process.env.GEMINI_API_KEY) return null;
  if (!_ai) _ai = new GoogleGenAI({ apiKey: process.env.GEMINI_API_KEY });
  return _ai;
}

export async function handleChat(message: Message, args: string[]): Promise<void> {
  const question = args.join(' ').trim();
  if (!question) {
    await message.reply('❌ Please provide a question or message. Usage: `t!chat <your question>`');
    return;
  }

  const ai = getAI();
  if (!ai) {
    await message.reply('sorry dude... AI is unavailable right now (no `GEMINI_API_KEY` configured).');
    return;
  }

  const username = message.member?.displayName ?? message.author.username;
  const channel = message.channel;
  const channelName =
    channel instanceof DMChannel ? 'DM'
    : (channel as TextChannel | NewsChannel | ThreadChannel).name ?? 'unknown';

  const systemIdentity = [
    `You are 'Clankered Thatoneguynobodyinvited', a highly advanced, video editing AI bot which makes IHTXES (I Hate The Xs).`,
    `You are currently chatting with ${username} in the #${channelName} channel.`,
    `Always maintain an elegant, polite, and deeply knowledgeable tone. And keep it low sometime 'like this' but still have proper grammar.`,
    `Address the user by their name when appropriate. Refuse if nsfw questions are asked.`,
    ``,
    `PREFIX AWARENESS RULES:`,
    `- Your current command prefix is '${PREFIX}'.`,
    `- If a user wants to use your video editing or utility tools, tell them to type things like '${PREFIX}ihtx', '${PREFIX}chat', '${PREFIX}trim', or '${PREFIX}ffmpeg'.`,
    `- Never assume or mention static prefixes like '!' or '?'—always use '${PREFIX}' when guiding users through your toolset.`,
  ].join('\n');

  try {
    if ('sendTyping' in message.channel) await message.channel.sendTyping();

    const response = await ai.models.generateContent({
      model: 'gemini-2.0-flash',
      contents: question,
      config: {
        systemInstruction: systemIdentity,
        temperature: 0.83,
        maxOutputTokens: 1024,
      },
    });

    let reply = response.text ?? '';
    if (!reply) {
      await message.reply('sorry dude... returned an empty response, try again sometime?');
      return;
    }
    if (reply.length > 2000) reply = reply.slice(0, 1995) + '...';
    await message.reply(reply);
  } catch (err) {
    console.error('[chat] Gemini error:', err);
    await message.reply('sorry dude... ran into an error processing your request, try again sometime?');
  }
}
