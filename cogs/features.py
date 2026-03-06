from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands, tasks


class FeaturesCog(commands.Cog):
    FEATURE_LABELS: dict[str, str] = {
        "moderation": "Moderation",
        "gamification": "Gamification",
        "ai_image": "AI Image",
        "translation": "Translation",
        "scheduled_messages": "Scheduled Messages",
    }

    FEATURE_CHOICES = [
        app_commands.Choice(name="Moderation", value="moderation"),
        app_commands.Choice(name="Gamification", value="gamification"),
        app_commands.Choice(name="AI Image", value="ai_image"),
        app_commands.Choice(name="Translation", value="translation"),
        app_commands.Choice(name="Scheduled Messages", value="scheduled_messages"),
    ]

    FEATURE_SLASH_COMMANDS: dict[str, tuple[str, ...]] = {
        "moderation": (
            "mod-exclude-add",
            "mod-exclude-remove",
            "mod-exclude-list",
            "mod-word-add",
            "mod-word-remove",
            "mod-word-list",
        ),
        "gamification": (
            "xp",
            "xp_leaderboard",
            "levels_show",
            "level_set",
            "levels_reset",
            "rep",
            "rep_give",
            "rep_add",
            "rep_remove",
        ),
        "scheduled_messages": (
            "schedule_create",
            "schedule_list",
            "schedule_delete",
            "schedule_pause",
            "schedule_resume",
            "schedule_edit",
        ),
    }

    FEATURE_MESSAGE_COMMANDS: dict[str, tuple[str, ...]] = {
        "ai_image": (
            "Generate AI Image",
        ),
        "translation": (
            "Translate (Private)",
            "Translate",
        ),
    }

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        if not self.refresh_feature_command_visibility.is_running():
            self.refresh_feature_command_visibility.start()

    async def cog_unload(self):
        if self.refresh_feature_command_visibility.is_running():
            self.refresh_feature_command_visibility.cancel()

    @tasks.loop(minutes=3)
    async def refresh_feature_command_visibility(self):
        for guild in self.bot.guilds:
            try:
                await self._sync_guild_feature_command_visibility(guild)
            except Exception as error:
                print(f"[WARN] Features: periodic command visibility sync failed for guild {guild.id}: {error}")

    @refresh_feature_command_visibility.before_loop
    async def before_refresh_feature_command_visibility(self):
        await self.bot.wait_until_ready()

    async def _sync_guild_feature_command_visibility(self, guild: discord.Guild):
        mongo_cog = await self._get_mongo_cog()
        if mongo_cog is None:
            return

        features = await mongo_cog.get_guild_features(guild.id)
        disabled_features = {
            key
            for key, value in (features or {}).items()
            if isinstance(value, dict) and value.get("enabled", True) is False
        }

        self.bot.tree.copy_global_to(guild=guild)

        for feature_key, command_names in self.FEATURE_SLASH_COMMANDS.items():
            if feature_key not in disabled_features:
                continue
            for command_name in command_names:
                self.bot.tree.remove_command(command_name, guild=guild)

        for feature_key, command_names in self.FEATURE_MESSAGE_COMMANDS.items():
            if feature_key not in disabled_features:
                continue
            for command_name in command_names:
                self.bot.tree.remove_command(
                    command_name,
                    guild=guild,
                    type=discord.AppCommandType.message,
                )

        await self.bot.tree.sync(guild=guild)

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            try:
                await self._sync_guild_feature_command_visibility(guild)
            except Exception as error:
                print(f"[WARN] Features: command visibility sync failed for guild {guild.id}: {error}")

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        try:
            await self._sync_guild_feature_command_visibility(guild)
        except Exception as error:
            print(f"[WARN] Features: initial command visibility sync failed for guild {guild.id}: {error}")

    async def _get_mongo_cog(self):
        return self.bot.get_cog("MongoDbCog")

    @staticmethod
    def _normalize_feature_rows(raw_features: dict) -> list[tuple[str, bool]]:
        rows: list[tuple[str, bool]] = []
        ordered_keys = [
            "moderation",
            "gamification",
            "ai_image",
            "translation",
            "scheduled_messages",
        ]

        for key in ordered_keys:
            value = raw_features.get(key, {}) if isinstance(raw_features, dict) else {}
            enabled = bool(value.get("enabled", True)) if isinstance(value, dict) else True
            rows.append((key, enabled))

        return rows

    async def _render_features_embed(self, guild_id: int, guild_name: str) -> discord.Embed | None:
        mongo_cog = await self._get_mongo_cog()
        if mongo_cog is None:
            return None

        features = await mongo_cog.get_guild_features(guild_id)
        rows = self._normalize_feature_rows(features)

        lines = []
        for key, enabled in rows:
            label = self.FEATURE_LABELS.get(key, key)
            icon = "✅" if enabled else "❌"
            lines.append(f"{icon} **{label}**")

        embed = discord.Embed(
            title=f"Feature Settings · {guild_name}",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        return embed

    async def _set_feature(
        self,
        interaction: discord.Interaction,
        feature_key: str,
        enabled: bool,
    ):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only administrators can change feature settings.", ephemeral=True)
            return

        mongo_cog = await self._get_mongo_cog()
        if mongo_cog is None:
            await interaction.response.send_message("Feature controls are unavailable right now.", ephemeral=True)
            return

        await mongo_cog.set_guild_feature_enabled(
            guild_id=interaction.guild.id,
            guild_name=interaction.guild.name,
            installer_user_id=interaction.user.id,
            feature_key=feature_key,
            enabled=enabled,
        )

        try:
            await self._sync_guild_feature_command_visibility(interaction.guild)
        except Exception as error:
            print(f"[WARN] Features: command visibility resync failed for guild {interaction.guild.id}: {error}")

        label = self.FEATURE_LABELS.get(feature_key, feature_key)
        feature_state_word = "added" if enabled else "removed"
        command_state_word = "enabled" if enabled else "disabled"
        embed = await self._render_features_embed(interaction.guild.id, interaction.guild.name)

        message = (
            f"The feature **{label}** has been **{feature_state_word}** "
            f"and all related slash commands **{command_state_word}**."
        )
        await interaction.response.send_message(message, embed=embed, ephemeral=True)

    @app_commands.command(name="features", description="Show enabled/disabled feature settings for this server")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def features(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        embed = await self._render_features_embed(interaction.guild.id, interaction.guild.name)
        if embed is None:
            await interaction.response.send_message("Feature controls are unavailable right now.", ephemeral=True)
            return

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="feature-enable", description="Enable a feature for this server")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(feature="Feature to enable")
    @app_commands.choices(feature=FEATURE_CHOICES)
    async def feature_enable(self, interaction: discord.Interaction, feature: app_commands.Choice[str]):
        await self._set_feature(interaction, feature.value, True)

    @app_commands.command(name="feature-disable", description="Disable a feature for this server")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(feature="Feature to disable")
    @app_commands.choices(feature=FEATURE_CHOICES)
    async def feature_disable(self, interaction: discord.Interaction, feature: app_commands.Choice[str]):
        await self._set_feature(interaction, feature.value, False)


async def setup(bot: commands.Bot):
    await bot.add_cog(FeaturesCog(bot))
