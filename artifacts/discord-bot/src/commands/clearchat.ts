import { Message } from 'discord.js';

export async function handleClearchat(message: Message): Promise<void> {
  await message.reply('alr chat wiped 🥀 not like i remembered anything anyway');
}
