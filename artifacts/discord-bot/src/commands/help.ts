import { Message, EmbedBuilder, GuildPremiumTier } from 'discord.js';
import { getMaxRepetitions, getUploadLimitBytes, formatBytes } from '../utils/limits.js';
import { LIMITS, PREFIX } from '../config.js';

export async function handleHelp(message: Message, ownerId: string): Promise<void> {
  const guild = message.guild ?? null;
  const isOwner = ownerId !== '' && message.author.id === ownerId;
  const maxReps = getMaxRepetitions(message.author.id, ownerId, guild);
  const uploadLimit = getUploadLimitBytes(guild);
  const boosted = guild && guild.premiumTier >= GuildPremiumTier.Tier1;
  const boostBonus = boosted ? ` +${LIMITS.BOOST_BONUS} boost` : '';
  const baseReps = isOwner ? LIMITS.OWNER_MAX_REPS : LIMITS.NON_OWNER_MAX_REPS;

  const embed = new EmbedBuilder()
    .setColor(0x5865f2)
    .setAuthor({
      name: isOwner ? `👑 ${message.author.displayName}` : message.author.displayName,
      iconURL: message.author.displayAvatarURL(),
    })
    .setTitle('IHTX Bot — All Commands')
    .setDescription(`Prefix: \`${PREFIX}\`  •  All commands start with \`t!\``)
    .addFields(
      {
        name: '🎬 Media',
        value: [
          `\`${PREFIX}download <url>\` — Download a video via yt-dlp`,
          `\`${PREFIX}multipitchihtx [opts]\` — Multi-voice pitch shifting on a video attachment`,
        ].join('\n'),
      },
      {
        name: '🎮 Games',
        value: [
          `\`${PREFIX}coinflip\` (alias: \`cf\`) — Flip a coin`,
          `\`${PREFIX}dice [expr]\` (alias: \`roll\`) — Roll dice, e.g. \`2d6\`, \`d20\`, \`3d8+5\``,
          `\`${PREFIX}rps <rock|paper|scissors>\` — Rock Paper Scissors vs bot`,
          `\`${PREFIX}8ball <question>\` — Ask the Magic 8-Ball`,
          `\`${PREFIX}slots\` — Spin the slot machine`,
          `\`${PREFIX}roulette <red|black|green|0-36>\` — Place a roulette bet`,
          `\`${PREFIX}choose <a | b | c>\` (alias: \`pick\`) — Pick a random option`,
          `\`${PREFIX}trivia\` — Answer a random trivia question`,
        ].join('\n'),
      },
      {
        name: `\`${PREFIX}multipitchihtx\` options`,
        value: [
          `\`repetitions=<n>\` — pitch layers (default: 20, **max: ${baseReps}${boostBonus}**)`,
          '`length=<n>` — pitch spread in semitones, 0.01–999 (default: 0.4)',
          '`engine=<r2|r3|r4>\` — Rubber Band engine (default: r3)',
          '`window=<long|short>\` — window mode (default: long)',
        ].join('\n'),
      },
      {
        name: '📊 Your Limits',
        value: [
          `Role: **${isOwner ? '👑 Owner' : 'User'}**`,
          `Server boost: **${boosted ? `Tier ${guild!.premiumTier} ✅` : 'None'}**`,
          `Max repetitions: **${maxReps}**`,
          `Max upload: **${formatBytes(uploadLimit)}**`,
        ].join('\n'),
      },
    )
    .setFooter({ text: 'IHTX Bot (TypeScript) • yt-dlp, ffmpeg, Rubber Band' });

  await message.reply({ embeds: [embed] });
}
