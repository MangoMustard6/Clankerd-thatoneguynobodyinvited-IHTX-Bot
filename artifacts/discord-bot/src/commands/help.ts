import { Message, EmbedBuilder, GuildPremiumTier } from 'discord.js';
import { getMaxRepetitions, getUploadLimitBytes, formatBytes } from '../utils/limits.js';
import { LIMITS, PREFIX } from '../config.js';

export async function handleHelp(message: Message, ownerId: string): Promise<void> {
  const guild = message.guild ?? null;
  const isOwner = ownerId !== '' && message.author.id === ownerId;
  const maxReps = getMaxRepetitions(message.author.id, ownerId, guild);
  const uploadLimit = getUploadLimitBytes(guild);
  const boosted = guild && guild.premiumTier >= GuildPremiumTier.Tier1;
  const boostBonus = boosted ? ` (+${LIMITS.BOOST_BONUS} boost)` : '';
  const baseReps = isOwner ? LIMITS.OWNER_MAX_REPS : LIMITS.NON_OWNER_MAX_REPS;

  const embed = new EmbedBuilder()
    .setColor(0x5865f2)
    .setTitle('IHTX Bot — Commands')
    .setDescription(`Prefix: \`${PREFIX}\`  •  All commands start with \`t!\``)
    .addFields(
      {
        name: `\`${PREFIX}download <url>\``,
        value: [
          'Download a video via yt-dlp and send it here.',
          '• URL must be `http://` or `https://`',
          '• Best quality mp4',
          `• Upload limit: **${formatBytes(uploadLimit)}**`,
        ].join('\n'),
      },
      {
        name: `\`${PREFIX}multipitchihtx [options]\``,
        value: [
          'Apply multi-voice pitch shifting to an attached video.',
          'Attach a video file (`.mp4 .mov .mkv .webm .avi`) to your message.',
          '',
          '**Options:**',
          `\`repetitions=<n>\` — pitch layers (default: 20, min: 1, **max: ${maxReps}**${boostBonus ? ` — base ${baseReps}${boostBonus}` : ''})`,
          '`length=<n>` — pitch spread in semitones, 0.01–999 (default: 0.4)',
          '`engine=<r2|r3|r4>` — Rubber Band engine (default: r3)',
          '`window=<long|short>` — window mode (default: long)',
          '',
          '**Example:**',
          `\`\`\`${PREFIX}multipitchihtx repetitions=10 length=0.6 engine=r3 window=long\`\`\``,
          '**Output:** WAV audio file only',
          `**Upload limit:** ${formatBytes(uploadLimit)}`,
        ].join('\n'),
      },
      {
        name: `\`${PREFIX}help\``,
        value: 'Show this message.',
      },
    )
    .addFields({
      name: '📊 Your Limits',
      value: [
        `Role: **${isOwner ? 'Owner' : 'User'}**`,
        `Server boost: **${boosted ? `Tier ${guild!.premiumTier} ✅` : 'None'}**`,
        `Max repetitions: **${maxReps}**`,
        `Max upload: **${formatBytes(uploadLimit)}**`,
      ].join('\n'),
      inline: false,
    })
    .setFooter({ text: 'IHTX Bot (TypeScript) • Uses yt-dlp, ffmpeg, Rubber Band' });

  await message.reply({ embeds: [embed] });
}
