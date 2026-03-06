from __future__ import annotations

import os
import random
import time

import discord
from discord import app_commands
from discord.ext import commands


class GamificationCog(commands.Cog):
    DEFAULT_LEADERBOARD_CHANNEL_NAME = "leaderboard"
    LEGACY_LEADERBOARD_CHANNEL_NAME = "discobot-leaderboard"
    DEFAULT_CATEGORY_NAME = "DiscoBot"
    LEADERBOARD_TITLE = "🏆 DiscoBot Leaderboards"
    XP_COOLDOWN_SECONDS = 45
    REP_GIVE_COOLDOWN_SECONDS = 24 * 60 * 60
    LEADERBOARD_UPDATE_INTERVAL_SECONDS = 180
    LEADERBOARD_SIZE = 10

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_xp_award_by_user: dict[tuple[int, int], float] = {}
        self._last_leaderboard_update_by_guild: dict[int, float] = {}
        self.single_user_mode = os.environ.get("SINGLE_USER_MODE", "false").lower() == "true"
        self._channel_settings_cache: dict[str, str] | None = None
        self._channel_settings_loaded_at: float = 0.0

    async def _get_channel_settings(self) -> dict[str, str]:
        now = time.time()
        if self._channel_settings_cache is not None and (now - self._channel_settings_loaded_at) <= 120:
            return self._channel_settings_cache

        settings = {
            "category_name": self.DEFAULT_CATEGORY_NAME,
            "leaderboard_channel_name": self.DEFAULT_LEADERBOARD_CHANNEL_NAME,
        }

        mongo_cog = await self._get_mongo_cog()
        if mongo_cog is not None:
            try:
                owner_settings = await mongo_cog.get_bot_owner_settings()
                gamification_settings = (
                    owner_settings.get("channels", {}).get("gamification", {})
                    if isinstance(owner_settings, dict)
                    else {}
                )

                settings["category_name"] = str(gamification_settings.get("category_name") or settings["category_name"])
                settings["leaderboard_channel_name"] = str(
                    gamification_settings.get("leaderboard_channel_name") or settings["leaderboard_channel_name"]
                )
            except Exception as error:
                print(f"[WARN] Gamification: Failed loading owner channel settings, using defaults: {error}")

        self._channel_settings_cache = settings
        self._channel_settings_loaded_at = now
        return settings

    async def _get_mongo_cog(self):
        return self.bot.get_cog("MongoDbCog")

    @staticmethod
    def _message_is_eligible(message: discord.Message) -> bool:
        if message.content and message.content.strip():
            return True
        if message.attachments or message.stickers:
            return True
        return False

    @staticmethod
    def _format_rank_lines(
        leaderboard_rows: list[dict],
        value_key: str,
        value_label: str,
        include_level: bool,
        level_name_by_level: dict[int, str] | None = None,
    ) -> str:
        if not leaderboard_rows:
            return "No entries yet."

        lines: list[str] = []
        medal_by_position = {1: "🥇", 2: "🥈", 3: "🥉"}
        for index, row in enumerate(leaderboard_rows, start=1):
            medal = medal_by_position.get(index, f"`#{index}`")
            user_id = row.get("user_id", 0)
            value = row.get(value_key, 0)
            if include_level:
                level = row.get("level", 0)
                level_name = (
                    level_name_by_level.get(int(level), str(row.get("level_name", "Newcomer")))
                    if level_name_by_level is not None
                    else str(row.get("level_name", "Newcomer"))
                )
                lines.append(f"{medal} <@{user_id}> — Level **{level}** ({level_name}), {value_label} **{value}**")
            else:
                lines.append(f"{medal} <@{user_id}> — {value_label} **{value}**")

        return "\n".join(lines)

    @staticmethod
    def _format_leaderboard_embed(
        guild: discord.Guild,
        xp_rows: list[dict],
        reputation_rows: list[dict],
        level_name_by_level: dict[int, str],
    ) -> discord.Embed:
        embed = discord.Embed(
            title=GamificationCog.LEADERBOARD_TITLE,
            description=f"Top {GamificationCog.LEADERBOARD_SIZE} in {guild.name} (XP + Reputation)",
            color=discord.Color.gold(),
        )

        xp_lines = GamificationCog._format_rank_lines(
            xp_rows,
            value_key="xp",
            value_label="XP",
            include_level=True,
            level_name_by_level=level_name_by_level,
        )
        rep_lines = GamificationCog._format_rank_lines(
            reputation_rows,
            value_key="reputation",
            value_label="Reputation",
            include_level=False,
        )

        embed.add_field(name="XP Rankings", value=xp_lines, inline=False)
        embed.add_field(name="Reputation Rankings", value=rep_lines, inline=False)
        embed.set_footer(text="Leaderboard updates automatically")
        return embed

    @staticmethod
    def _build_level_name_lookup(level_config: list[dict]) -> dict[int, str]:
        lookup: dict[int, str] = {}
        for row in level_config:
            try:
                lookup[int(row.get("level", 0))] = str(row.get("name", "Newcomer"))
            except (TypeError, ValueError):
                continue
        if 0 not in lookup:
            lookup[0] = "Newcomer"
        return lookup

    async def _ensure_leaderboard_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        if guild.me is None or not guild.me.guild_permissions.manage_channels:
            return None

        channel_settings = await self._get_channel_settings()
        leaderboard_channel_name = channel_settings["leaderboard_channel_name"]
        category = await self._ensure_discobot_category(guild)

        existing = discord.utils.get(guild.text_channels, name=leaderboard_channel_name)
        legacy = discord.utils.get(guild.text_channels, name=self.LEGACY_LEADERBOARD_CHANNEL_NAME)
        target_channel = existing or legacy

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }

        if target_channel:
            needs_update = (
                target_channel.name != leaderboard_channel_name
                or (category is not None and target_channel.category_id != category.id)
            )
            if needs_update:
                try:
                    await target_channel.edit(
                        name=leaderboard_channel_name,
                        category=category,
                        sync_permissions=False,
                        overwrites=overwrites,
                        reason="Normalize DiscoBot leaderboard channel",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    return target_channel

            return target_channel

        try:
            return await guild.create_text_channel(
                leaderboard_channel_name,
                category=category,
                overwrites=overwrites,
                reason="Create leaderboard channel for DiscoBot gamification",
            )
        except (discord.Forbidden, discord.HTTPException):
            return None

    async def _ensure_discobot_category(self, guild: discord.Guild) -> discord.CategoryChannel | None:
        channel_settings = await self._get_channel_settings()
        category_name = channel_settings["category_name"]

        existing = discord.utils.get(guild.categories, name=category_name)
        if existing:
            try:
                await existing.edit(
                    overwrites={},
                    reason="Ensure DiscoBot category is visible to all users",
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
            return existing

        if guild.me is None or not guild.me.guild_permissions.manage_channels:
            return None

        try:
            return await guild.create_category(
                category_name,
                reason="Create DiscoBot category for bot channels",
            )
        except (discord.Forbidden, discord.HTTPException):
            return None

    async def _find_existing_leaderboard_message(self, channel: discord.TextChannel) -> discord.Message | None:
        try:
            async for message in channel.history(limit=40):
                if message.author.id != self.bot.user.id:
                    continue
                if not message.embeds:
                    continue
                if message.embeds[0].title == self.LEADERBOARD_TITLE:
                    return message
        except (discord.Forbidden, discord.HTTPException):
            return None

        return None

    async def _update_leaderboard_for_guild(self, guild: discord.Guild, force: bool = False):
        now = time.time()
        last_update = self._last_leaderboard_update_by_guild.get(guild.id, 0)
        if not force and now - last_update < self.LEADERBOARD_UPDATE_INTERVAL_SECONDS:
            return

        mongo_cog = await self._get_mongo_cog()
        if mongo_cog is None:
            return

        channel = await self._ensure_leaderboard_channel(guild)
        if channel is None:
            return

        try:
            xp_rows = await mongo_cog.get_guild_xp_leaderboard(guild.id, limit=self.LEADERBOARD_SIZE)
            reputation_rows = await mongo_cog.get_guild_reputation_leaderboard(guild.id, limit=self.LEADERBOARD_SIZE)
            level_config = await mongo_cog.get_guild_gamification_levels(guild.id)
        except Exception as error:
            print(f"[WARN] Gamification: Failed retrieving leaderboard for guild {guild.name} ({guild.id}): {error}")
            return

        level_name_by_level = self._build_level_name_lookup(level_config)
        embed = self._format_leaderboard_embed(
            guild,
            xp_rows=xp_rows,
            reputation_rows=reputation_rows,
            level_name_by_level=level_name_by_level,
        )
        existing_message = await self._find_existing_leaderboard_message(channel)

        try:
            if existing_message:
                await existing_message.edit(embed=embed)
            else:
                await channel.send(embed=embed)
            self._last_leaderboard_update_by_guild[guild.id] = now
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"[WARN] Gamification: Failed updating leaderboard message in guild {guild.name} ({guild.id}): {error}")

    @staticmethod
    def _get_level_for_interactions(level_config: list[dict], interactions_count: int) -> tuple[int, str]:
        selected_level = 0
        selected_name = "Newcomer"
        interactions = max(int(interactions_count), 0)

        sorted_levels = sorted(level_config, key=lambda row: int(row.get("interactions_required", 0)))
        for row in sorted_levels:
            required = max(int(row.get("interactions_required", 0)), 0)
            if interactions >= required:
                selected_level = int(row.get("level", selected_level))
                selected_name = str(row.get("name", selected_name))
            else:
                break

        return selected_level, selected_name

    async def _award_xp(self, message: discord.Message):
        if message.guild is None:
            return

        mongo_cog = await self._get_mongo_cog()
        if mongo_cog is None:
            return

        xp_gain = random.randint(8, 16)
        try:
            progress = await mongo_cog.increment_user_xp(
                guild_id=message.guild.id,
                guild_name=message.guild.name,
                user_id=message.author.id,
                username=str(message.author),
                xp_gain=xp_gain,
            )
            level_config = await mongo_cog.get_guild_gamification_levels(message.guild.id)
        except Exception as error:
            print(f"[WARN] Gamification: Failed awarding XP in guild {message.guild.name} ({message.guild.id}): {error}")
            return

        xp_total = int(progress.get("xp", 0))
        old_level = int(progress.get("level", 0))
        new_level, new_level_name = self._get_level_for_interactions(level_config, xp_total)

        if new_level != old_level or str(progress.get("level_name", "")) != new_level_name:
            try:
                await mongo_cog.set_user_level_info(
                    guild_id=message.guild.id,
                    user_id=message.author.id,
                    level=new_level,
                    level_name=new_level_name,
                )
            except Exception as error:
                print(
                    f"[WARN] Gamification: Failed updating level info for user {message.author.id} "
                    f"in guild {message.guild.id}: {error}"
                )

        if new_level > old_level:
            try:
                await message.channel.send(
                    f"🎉 {message.author.mention} leveled up to **Level {new_level}** ({new_level_name})!"
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

        await self._update_leaderboard_for_guild(message.guild)

    @app_commands.command(name="xp", description="Show XP and level for you or another member")
    @app_commands.guild_only()
    async def xp(self, interaction: discord.Interaction, member: discord.Member | None = None):
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        target_member = member or interaction.user
        mongo_cog = await self._get_mongo_cog()
        if mongo_cog is None:
            await interaction.response.send_message("Gamification is unavailable right now.", ephemeral=True)
            return

        stats = await mongo_cog.get_user_gamification_stats(interaction.guild.id, target_member.id)
        if stats is None:
            await interaction.response.send_message(
                f"{target_member.mention} has no XP yet. Start chatting to earn XP.",
                ephemeral=True,
            )
            return

        level_config = await mongo_cog.get_guild_gamification_levels(interaction.guild.id)
        level_name_by_level = self._build_level_name_lookup(level_config)
        current_level = int(stats.get("level", 0))
        current_level_name = level_name_by_level.get(current_level, str(stats.get("level_name", "Newcomer")))

        embed = discord.Embed(title="⭐ XP Profile", color=discord.Color.blurple())
        embed.add_field(name="Member", value=target_member.mention, inline=False)
        embed.add_field(name="Level", value=f"{current_level} ({current_level_name})", inline=True)
        embed.add_field(name="XP", value=str(stats.get("xp", 0)), inline=True)
        embed.add_field(name="Reputation", value=str(stats.get("reputation", 0)), inline=True)
        embed.add_field(name="Messages Counted", value=str(stats.get("message_count", 0)), inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="xp_leaderboard", description="Show the guild XP leaderboard")
    @app_commands.guild_only()
    async def xp_leaderboard(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        mongo_cog = await self._get_mongo_cog()
        if mongo_cog is None:
            await interaction.response.send_message("Gamification is unavailable right now.", ephemeral=True)
            return

        xp_rows = await mongo_cog.get_guild_xp_leaderboard(interaction.guild.id, limit=self.LEADERBOARD_SIZE)
        reputation_rows = await mongo_cog.get_guild_reputation_leaderboard(interaction.guild.id, limit=self.LEADERBOARD_SIZE)
        level_config = await mongo_cog.get_guild_gamification_levels(interaction.guild.id)
        level_name_by_level = self._build_level_name_lookup(level_config)
        embed = self._format_leaderboard_embed(
            interaction.guild,
            xp_rows=xp_rows,
            reputation_rows=reputation_rows,
            level_name_by_level=level_name_by_level,
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="levels_show", description="Show the current level configuration")
    @app_commands.guild_only()
    async def levels_show(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        mongo_cog = await self._get_mongo_cog()
        if mongo_cog is None:
            await interaction.response.send_message("Level configuration is unavailable right now.", ephemeral=True)
            return

        levels = await mongo_cog.get_guild_gamification_levels(interaction.guild.id)
        lines = [
            f"- Level {int(row.get('level', 0))} ({str(row.get('name', 'Level'))}): {int(row.get('interactions_required', 0))} XP"
            for row in levels
        ]
        await interaction.response.send_message("Current levels:\n" + "\n".join(lines), ephemeral=True)

    @app_commands.command(name="level_set", description="Admin: set or update a level definition")
    @app_commands.guild_only()
    async def level_set(
        self,
        interaction: discord.Interaction,
        level: app_commands.Range[int, 0, 100],
        level_name: app_commands.Range[str, 1, 24],
        interactions_required: app_commands.Range[int, 0, 1000000],
    ):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only administrators can change level configuration.", ephemeral=True)
            return

        mongo_cog = await self._get_mongo_cog()
        if mongo_cog is None:
            await interaction.response.send_message("Level configuration is unavailable right now.", ephemeral=True)
            return

        updated = await mongo_cog.upsert_guild_gamification_level(
            guild_id=interaction.guild.id,
            guild_name=interaction.guild.name,
            level=int(level),
            level_name=str(level_name).strip(),
            interactions_required=int(interactions_required),
        )

        recalculated_count = await mongo_cog.recalculate_guild_user_levels(
            guild_id=interaction.guild.id,
            level_config=updated,
        )

        await interaction.response.send_message(
            f"✅ Updated level {level} to '{level_name}' at {interactions_required} XP. "
            f"Total levels configured: {len(updated)}. Recalculated users: {recalculated_count}.",
            ephemeral=True,
        )
        await self._update_leaderboard_for_guild(interaction.guild, force=True)

    @app_commands.command(name="levels_reset", description="Admin: reset levels to defaults")
    @app_commands.guild_only()
    async def levels_reset(self, interaction: discord.Interaction):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only administrators can change level configuration.", ephemeral=True)
            return

        mongo_cog = await self._get_mongo_cog()
        if mongo_cog is None:
            await interaction.response.send_message("Level configuration is unavailable right now.", ephemeral=True)
            return

        defaults = await mongo_cog.reset_guild_gamification_levels(interaction.guild.id, interaction.guild.name)
        recalculated_count = await mongo_cog.recalculate_guild_user_levels(
            guild_id=interaction.guild.id,
            level_config=defaults,
        )
        await interaction.response.send_message(
            f"✅ Level configuration reset to defaults. Recalculated users: {recalculated_count}.",
            ephemeral=True,
        )
        await self._update_leaderboard_for_guild(interaction.guild, force=True)

    @app_commands.command(name="rep", description="Show reputation for you or another member")
    @app_commands.guild_only()
    async def rep(self, interaction: discord.Interaction, member: discord.Member | None = None):
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        target_member = member or interaction.user
        mongo_cog = await self._get_mongo_cog()
        if mongo_cog is None:
            await interaction.response.send_message("Reputation is unavailable right now.", ephemeral=True)
            return

        stats = await mongo_cog.get_user_gamification_stats(interaction.guild.id, target_member.id)
        reputation = 0 if stats is None else int(stats.get("reputation", 0))
        await interaction.response.send_message(f"⭐ {target_member.mention} has **{reputation}** reputation.", ephemeral=True)

    @app_commands.command(name="rep_give", description="Give 1 reputation point to another member (daily cooldown)")
    @app_commands.guild_only()
    async def rep_give(self, interaction: discord.Interaction, member: discord.Member):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        if member.bot:
            await interaction.response.send_message("You cannot give reputation to bots.", ephemeral=True)
            return

        if member.id == interaction.user.id and not self.single_user_mode:
            await interaction.response.send_message("You cannot give reputation to yourself.", ephemeral=True)
            return

        mongo_cog = await self._get_mongo_cog()
        if mongo_cog is None:
            await interaction.response.send_message("Reputation is unavailable right now.", ephemeral=True)
            return

        result = await mongo_cog.give_peer_reputation(
            guild_id=interaction.guild.id,
            guild_name=interaction.guild.name,
            giver_user_id=interaction.user.id,
            giver_username=str(interaction.user),
            target_user_id=member.id,
            target_username=str(member),
            cooldown_seconds=0 if self.single_user_mode else self.REP_GIVE_COOLDOWN_SECONDS,
        )

        if not bool(result.get("ok", False)):
            retry_after_seconds = int(result.get("retry_after_seconds", 0))
            retry_hours = max(retry_after_seconds // 3600, 0)
            retry_minutes = max((retry_after_seconds % 3600) // 60, 0)
            await interaction.response.send_message(
                f"You already gave reputation recently. Try again in about {retry_hours}h {retry_minutes}m.",
                ephemeral=True,
            )
            return

        new_rep = int(result.get("reputation", 0))
        await interaction.response.send_message(
            f"✅ You gave 1 reputation point to {member.mention}. They now have **{new_rep}** reputation."
        )
        await self._update_leaderboard_for_guild(interaction.guild, force=True)

    @app_commands.command(name="rep_add", description="Admin: add reputation points to a member")
    @app_commands.guild_only()
    async def rep_add(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        points: app_commands.Range[int, 1, 100],
    ):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only administrators can add reputation.", ephemeral=True)
            return

        mongo_cog = await self._get_mongo_cog()
        if mongo_cog is None:
            await interaction.response.send_message("Reputation is unavailable right now.", ephemeral=True)
            return

        result = await mongo_cog.adjust_user_reputation(
            guild_id=interaction.guild.id,
            guild_name=interaction.guild.name,
            user_id=member.id,
            username=str(member),
            delta=int(points),
            actor_user_id=interaction.user.id,
            actor_username=str(interaction.user),
        )
        await interaction.response.send_message(
            f"✅ Added **{points}** reputation to {member.mention}. Total reputation: **{result['reputation']}**."
        )
        await self._update_leaderboard_for_guild(interaction.guild, force=True)

    @app_commands.command(name="rep_remove", description="Admin: remove reputation points from a member")
    @app_commands.guild_only()
    async def rep_remove(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        points: app_commands.Range[int, 1, 100],
    ):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only administrators can remove reputation.", ephemeral=True)
            return

        mongo_cog = await self._get_mongo_cog()
        if mongo_cog is None:
            await interaction.response.send_message("Reputation is unavailable right now.", ephemeral=True)
            return

        result = await mongo_cog.adjust_user_reputation(
            guild_id=interaction.guild.id,
            guild_name=interaction.guild.name,
            user_id=member.id,
            username=str(member),
            delta=-int(points),
            actor_user_id=interaction.user.id,
            actor_username=str(interaction.user),
        )
        await interaction.response.send_message(
            f"✅ Removed **{points}** reputation from {member.mention}. Total reputation: **{result['reputation']}**."
        )
        await self._update_leaderboard_for_guild(interaction.guild, force=True)

    @commands.Cog.listener()
    async def on_ready(self):
        if self.single_user_mode:
            print("[WARN] Gamification: SINGLE_USER_MODE is enabled; rep self-award and cooldown checks are bypassed.")
        else:
            print("[INFO] Gamification: SINGLE_USER_MODE is disabled; standard rep anti-abuse checks are active.")

        for guild in self.bot.guilds:
            await self._update_leaderboard_for_guild(guild, force=True)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self._update_leaderboard_for_guild(guild, force=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.guild is None:
            return
        if not self._message_is_eligible(message):
            return

        key = (message.guild.id, message.author.id)
        now = time.time()
        last_award = self._last_xp_award_by_user.get(key, 0)
        if now - last_award < self.XP_COOLDOWN_SECONDS:
            return

        self._last_xp_award_by_user[key] = now
        await self._award_xp(message)


async def setup(bot: commands.Bot):
    await bot.add_cog(GamificationCog(bot))
