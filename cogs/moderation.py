from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands


class ModerationActionView(discord.ui.View):
    def __init__(self, cog: "ModerationCog", allow_enforcement: bool = True):
        super().__init__(timeout=None)
        self.cog = cog
        self.allow_enforcement = allow_enforcement

        if not self.allow_enforcement:
            self.kick_button.disabled = True
            self.ban_button.disabled = True

    @staticmethod
    def _extract_source_ids(embed: discord.Embed) -> tuple[int | None, int | None]:
        message_id = None
        channel_id = None

        for field in embed.fields:
            if field.name == "Source Message ID":
                try:
                    message_id = int(field.value)
                except (TypeError, ValueError):
                    message_id = None
            elif field.name == "Source Channel ID":
                try:
                    channel_id = int(field.value)
                except (TypeError, ValueError):
                    channel_id = None

        return message_id, channel_id

    @staticmethod
    def _replace_or_add_action_field(embed: discord.Embed, value: str):
        for index, field in enumerate(embed.fields):
            if field.name == "Moderator Action":
                embed.set_field_at(index, name="Moderator Action", value=value, inline=False)
                return

        embed.add_field(name="Moderator Action", value=value, inline=False)

    @staticmethod
    def _replace_or_add_field(embed: discord.Embed, field_name: str, value: str, inline: bool = True):
        for index, field in enumerate(embed.fields):
            if field.name == field_name:
                embed.set_field_at(index, name=field_name, value=value, inline=inline)
                return

        embed.add_field(name=field_name, value=value, inline=inline)

    async def _finalize(
        self,
        interaction: discord.Interaction,
        action_text: str,
        remove_original_message: bool,
        approved: bool,
        enforcement_action: str | None = None,
    ):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This action can only be used in a server.", ephemeral=True)
            return

        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only administrators can use these moderation actions.", ephemeral=True)
            return

        if interaction.message is None:
            await interaction.response.send_message("Could not find the moderation message.", ephemeral=True)
            return

        if not interaction.message.embeds:
            await interaction.response.send_message("Could not parse moderation details.", ephemeral=True)
            return

        embed = interaction.message.embeds[0].copy()
        source_message_id, source_channel_id = self._extract_source_ids(embed)
        source_user_id = self._extract_source_user_id(embed)
        reason_text = self._extract_reason_text(embed)

        removal_note = ""
        if remove_original_message and source_message_id and source_channel_id:
            channel = interaction.guild.get_channel(source_channel_id)
            if channel is None:
                try:
                    channel = await interaction.guild.fetch_channel(source_channel_id)
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    channel = None

            if isinstance(channel, discord.TextChannel):
                try:
                    original_message = await channel.fetch_message(source_message_id)
                    await original_message.delete()
                    removal_note = " Original message removed."
                except discord.NotFound:
                    removal_note = " Original message already removed."
                except (discord.Forbidden, discord.HTTPException):
                    removal_note = " Failed to remove original message due to permissions or API error."
            else:
                removal_note = " Could not access original channel to remove message."

        enforcement_note = ""
        if enforcement_action and source_user_id:
            enforcement_note = await self._apply_member_enforcement(
                interaction=interaction,
                source_user_id=source_user_id,
                enforcement_action=enforcement_action,
                reason_text=reason_text,
            )

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        moderator_tag = f"{interaction.user} ({interaction.user.id})"

        if source_user_id:
            updated_stats = await self.cog._record_moderation_decision(
                guild=interaction.guild,
                source_user_id=source_user_id,
                approved=approved,
            )
            if updated_stats:
                self._replace_or_add_field(
                    embed,
                    "Flagged Count",
                    str(updated_stats["moderation_flag_count"]),
                    inline=True,
                )
                self._replace_or_add_field(
                    embed,
                    "Approved Count",
                    str(updated_stats["moderation_approved_count"]),
                    inline=True,
                )
                self._replace_or_add_field(
                    embed,
                    "Rejected Count",
                    str(updated_stats["moderation_rejected_count"]),
                    inline=True,
                )

        self._replace_or_add_action_field(
            embed,
            f"{action_text} by {moderator_tag} at {timestamp}.{removal_note}{enforcement_note}",
        )

        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Approve Message", style=discord.ButtonStyle.success, custom_id="moderation:approve")
    async def approve_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._finalize(
            interaction,
            action_text="✅ Approved",
            remove_original_message=False,
            approved=True,
            enforcement_action=None,
        )

    @discord.ui.button(label="Remove Message", style=discord.ButtonStyle.danger, custom_id="moderation:remove")
    async def remove_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._finalize(
            interaction,
            action_text="🗑️ Removed",
            remove_original_message=True,
            approved=False,
            enforcement_action=None,
        )

    @discord.ui.button(label="Kick User", style=discord.ButtonStyle.secondary, custom_id="moderation:kick")
    async def kick_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._finalize(
            interaction,
            action_text="👢 Rejected + Kicked",
            remove_original_message=True,
            approved=False,
            enforcement_action="kick",
        )

    @discord.ui.button(label="Ban User", style=discord.ButtonStyle.secondary, custom_id="moderation:ban")
    async def ban_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._finalize(
            interaction,
            action_text="⛔ Rejected + Banned",
            remove_original_message=True,
            approved=False,
            enforcement_action="ban",
        )

    @staticmethod
    def _extract_source_user_id(embed: discord.Embed) -> int | None:
        for field in embed.fields:
            if field.name == "Source User ID":
                try:
                    return int(field.value)
                except (TypeError, ValueError):
                    return None

        return None

    @staticmethod
    def _extract_reason_text(embed: discord.Embed) -> str:
        for field in embed.fields:
            if field.name == "Reason":
                return field.value

        return "No reason provided."

    async def _resolve_target_user(
        self,
        interaction: discord.Interaction,
        source_user_id: int,
    ) -> tuple[discord.Member | None, discord.User | None]:
        member = interaction.guild.get_member(source_user_id) if interaction.guild else None
        if member is not None:
            return member, member

        try:
            fetched_user = await interaction.client.fetch_user(source_user_id)
            return None, fetched_user
        except (discord.NotFound, discord.HTTPException):
            return None, None

    @staticmethod
    def _build_direct_message(admin: discord.Member, guild_name: str, action_verb: str, reason_text: str) -> str:
        admin_profile_url = f"https://discord.com/users/{admin.id}"
        return (
            f"Administrator {admin.display_name} ({admin_profile_url}) has {action_verb} you due to recent comments "
            f"you have made in our {guild_name} server. If you feel this is undeserved or would like to contest, "
            f"please contact the administrator directly to discuss this issue.\n\n"
            f"Reason noted by moderation:\n{reason_text}\n\n"
            f"Regards,\n"
            f"The {guild_name} Admin Team"
        )

    async def _apply_member_enforcement(
        self,
        interaction: discord.Interaction,
        source_user_id: int,
        enforcement_action: str,
        reason_text: str,
    ) -> str:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return " Could not perform enforcement: guild context missing."

        bot_member = interaction.guild.me
        if bot_member is None:
            return " Could not perform enforcement: bot member unavailable."

        member, user = await self._resolve_target_user(interaction, source_user_id)
        if user is None:
            return " Could not resolve target user for enforcement."

        if interaction.guild.owner_id == source_user_id:
            return " Enforcement blocked: target user is the server owner. Please speak to them directly."

        if member is not None and member.guild_permissions.administrator:
            return " Enforcement blocked: target user is an administrator. Please speak to them directly."

        action_verb = "banned" if enforcement_action == "ban" else "kicked"
        dm_text = self._build_direct_message(interaction.user, interaction.guild.name, action_verb, reason_text)

        dm_note = ""
        try:
            await user.send(dm_text)
            dm_note = " User DM sent."
        except (discord.Forbidden, discord.HTTPException):
            dm_note = " Could not DM user."

        try:
            if enforcement_action == "kick":
                if not bot_member.guild_permissions.kick_members:
                    return " Kick failed: bot lacks Kick Members permission." + dm_note
                if member is None:
                    return " Kick failed: user is not currently in the server." + dm_note

                await member.kick(reason=f"Moderation action by {interaction.user} ({interaction.user.id})")
                await self._record_moderation_enforcement(
                    guild=interaction.guild,
                    source_user_id=source_user_id,
                    enforcement_action="kick",
                )
                return " User kicked from guild." + dm_note

            if not bot_member.guild_permissions.ban_members:
                return " Ban failed: bot lacks Ban Members permission." + dm_note

            await interaction.guild.ban(
                user,
                reason=f"Moderation action by {interaction.user} ({interaction.user.id})",
            )
            await self._record_moderation_enforcement(
                guild=interaction.guild,
                source_user_id=source_user_id,
                enforcement_action="ban",
            )
            return " User banned from guild." + dm_note
        except discord.Forbidden:
            return f" {action_verb.capitalize()} failed due to role hierarchy/permissions." + dm_note
        except discord.HTTPException:
            return f" {action_verb.capitalize()} failed due to Discord API error." + dm_note


class ModerationCog(commands.Cog):
    DEFAULT_MOD_CHANNEL_NAME = "admin-moderation"
    DEFAULT_MOD_CATEGORY_NAME = "DiscoBot-Admin"
    DEFAULT_MOD_CHANNEL_DESCRIPTION = (
        "Welcome to the DiscoBot Admin Moderation channel. This channel is only visible to administrators and will "
        "flag up any potentially offensive, rude or otherwise unwanted messages posted by users. You will be able "
        "to approve or reject and remove them. A log will be kept of how many moderated comments are made per user, "
        "and how many of these were approved/rejected, to make it easy to see if you have any specific users "
        "regularly posting offensive content. You will also have the opportunity to directly kick/ban a user from "
        "the guild if required and it will send them a direct message informing them why they were kicked/banned."
    )
    EXCLUSIONS_FILE = Path("data/moderation/excluded_channels.json")
    CUSTOM_TERMS_CACHE_TTL_SECONDS = 120

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.excluded_channel_ids_by_guild: dict[int, set[int]] = self._load_excluded_channels()
        self._manual_access_warned_guilds: set[int] = set()
        self._last_missing_permissions_by_guild: dict[int, tuple[str, ...]] = {}
        self.offensive_terms = {
            "idiot",
            "stupid",
            "dumb",
            "moron",
            "loser",
            "trash",
            "garbage",
            "hate you",
            "shut up",
            "fool",
        }
        self.offensive_patterns = [
            re.compile(r"\b(f+\s*u+\s*c+\s*k+)\b", re.IGNORECASE),
            re.compile(r"\b(a+\s*s+\s*s+h*o*l*e*)\b", re.IGNORECASE),
            re.compile(r"\b(b+i+t+c+h+)\b", re.IGNORECASE),
            re.compile(r"\b(d+i+c+k+)\b", re.IGNORECASE),
            re.compile(r"\b(s+h+i+t+)\b", re.IGNORECASE),
            re.compile(r"\b(k+y+s|kill\s+yourself)\b", re.IGNORECASE),
            re.compile(r"\b(go\s+to\s+hell)\b", re.IGNORECASE),
        ]
        self.custom_terms_cache_by_guild: dict[int, tuple[set[str], datetime]] = {}
        self._channel_settings_cache: dict[str, str] | None = None
        self._channel_settings_loaded_at: float = 0.0

        self.exclude_add_command = app_commands.Command(
            name="mod-exclude-add",
            description="Exclude a channel from moderation scanning",
            callback=self.mod_exclude_add,
        )
        self.exclude_remove_command = app_commands.Command(
            name="mod-exclude-remove",
            description="Remove a channel from moderation exclusions",
            callback=self.mod_exclude_remove,
        )
        self.exclude_list_command = app_commands.Command(
            name="mod-exclude-list",
            description="List channels excluded from moderation scanning",
            callback=self.mod_exclude_list,
        )
        self.word_add_command = app_commands.Command(
            name="mod-word-add",
            description="Add a custom moderation word or phrase",
            callback=self.mod_word_add,
        )
        self.word_remove_command = app_commands.Command(
            name="mod-word-remove",
            description="Remove a custom moderation word or phrase",
            callback=self.mod_word_remove,
        )
        self.word_list_command = app_commands.Command(
            name="mod-word-list",
            description="List custom moderation words for this server",
            callback=self.mod_word_list,
        )

    async def cog_load(self):
        self.bot.add_view(ModerationActionView(self))
        self.bot.tree.add_command(self.exclude_add_command)
        self.bot.tree.add_command(self.exclude_remove_command)
        self.bot.tree.add_command(self.exclude_list_command)
        self.bot.tree.add_command(self.word_add_command)
        self.bot.tree.add_command(self.word_remove_command)
        self.bot.tree.add_command(self.word_list_command)

    async def _get_moderation_channel_settings(self) -> dict[str, str]:
        now = time.time()
        if self._channel_settings_cache is not None and (now - self._channel_settings_loaded_at) <= 120:
            return self._channel_settings_cache

        settings = {
            "category_name": self.DEFAULT_MOD_CATEGORY_NAME,
            "channel_name": self.DEFAULT_MOD_CHANNEL_NAME,
            "channel_description": self.DEFAULT_MOD_CHANNEL_DESCRIPTION,
        }

        mongo_cog = self.bot.get_cog("MongoDbCog")
        if mongo_cog is not None:
            try:
                owner_settings = await mongo_cog.get_bot_owner_settings()
                moderation_settings = (
                    owner_settings.get("channels", {}).get("moderation", {})
                    if isinstance(owner_settings, dict)
                    else {}
                )

                settings["category_name"] = str(moderation_settings.get("category_name") or settings["category_name"])
                settings["channel_name"] = str(moderation_settings.get("channel_name") or settings["channel_name"])
                settings["channel_description"] = str(
                    moderation_settings.get("channel_description") or settings["channel_description"]
                )
            except Exception as error:
                print(f"[WARN] Moderation: Failed loading owner channel settings, using defaults: {error}")

        self._channel_settings_cache = settings
        self._channel_settings_loaded_at = now
        return settings

    def _log_manual_access_recovery(self, guild_id: int | None = None):
        if guild_id is not None and guild_id in self._manual_access_warned_guilds:
            return

        if guild_id is not None:
            self._manual_access_warned_guilds.add(guild_id)

        print(
            "[WARN] Moderation: Manual intervention required. Grant the bot temporary access to the DiscoBot "
            "category/channel (or delete/recreate them) so setup can complete."
        )

    def _log_missing_permissions(self, guild: discord.Guild):
        if guild.me is None:
            print(f"[WARN] Moderation: Unable to verify permissions in guild {guild.name} ({guild.id}).")
            return

        required_permissions = {
            "manage_channels": "Manage Channels",
            "view_channel": "View Channels",
            "send_messages": "Send Messages",
            "read_message_history": "Read Message History",
            "manage_messages": "Manage Messages",
            "kick_members": "Kick Members",
            "ban_members": "Ban Members",
        }

        missing: list[str] = []
        for attr_name, display_name in required_permissions.items():
            if not getattr(guild.me.guild_permissions, attr_name, False):
                missing.append(display_name)

        current_missing = tuple(sorted(missing))
        previous_missing = self._last_missing_permissions_by_guild.get(guild.id)

        if current_missing != previous_missing:
            self._last_missing_permissions_by_guild[guild.id] = current_missing

        if missing and current_missing == previous_missing:
            return

        if missing:
            print(
                f"[WARN] Moderation: Missing permissions in guild {guild.name} ({guild.id}): {', '.join(missing)}"
            )

    async def cog_unload(self):
        self.bot.tree.remove_command(self.exclude_add_command.name)
        self.bot.tree.remove_command(self.exclude_remove_command.name)
        self.bot.tree.remove_command(self.exclude_list_command.name)
        self.bot.tree.remove_command(self.word_add_command.name)
        self.bot.tree.remove_command(self.word_remove_command.name)
        self.bot.tree.remove_command(self.word_list_command.name)

    def _load_excluded_channels(self) -> dict[int, set[int]]:
        if not self.EXCLUSIONS_FILE.exists():
            return {}

        try:
            with self.EXCLUSIONS_FILE.open("r", encoding="utf-8") as file:
                raw_data = json.load(file)
        except (json.JSONDecodeError, OSError):
            return {}

        parsed: dict[int, set[int]] = {}
        for guild_id_str, channel_ids in raw_data.items():
            try:
                guild_id = int(guild_id_str)
            except (TypeError, ValueError):
                continue

            valid_channel_ids: set[int] = set()
            if isinstance(channel_ids, list):
                for channel_id in channel_ids:
                    try:
                        valid_channel_ids.add(int(channel_id))
                    except (TypeError, ValueError):
                        continue

            if valid_channel_ids:
                parsed[guild_id] = valid_channel_ids

        return parsed

    def _save_excluded_channels(self):
        self.EXCLUSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)

        serializable = {
            str(guild_id): sorted(channel_ids)
            for guild_id, channel_ids in self.excluded_channel_ids_by_guild.items()
            if channel_ids
        }

        with self.EXCLUSIONS_FILE.open("w", encoding="utf-8") as file:
            json.dump(serializable, file, indent=2)

    def _get_excluded_channels(self, guild_id: int) -> set[int]:
        return self.excluded_channel_ids_by_guild.get(guild_id, set())

    @staticmethod
    def _normalize_custom_term(term_raw: str) -> str | None:
        term = str(term_raw or "").strip().lower()
        if not term:
            return None

        term = " ".join(term.split())
        if len(term) < 2 or len(term) > 64:
            return None

        return term

    async def _get_custom_terms_for_guild(self, guild_id: int) -> set[str]:
        now = datetime.now(timezone.utc)
        cached = self.custom_terms_cache_by_guild.get(guild_id)
        if cached is not None:
            terms, loaded_at = cached
            if (now - loaded_at).total_seconds() <= self.CUSTOM_TERMS_CACHE_TTL_SECONDS:
                return set(terms)

        mongo_cog = self.bot.get_cog("MongoDbCog")
        if mongo_cog is None:
            return set()

        try:
            terms = await mongo_cog.get_guild_custom_moderation_terms(guild_id)
        except Exception as error:
            print(f"[WARN] Moderation: Failed loading custom moderation terms from MongoDB: {error}")
            return set()

        normalized = {term for term in terms if isinstance(term, str) and term}
        self.custom_terms_cache_by_guild[guild_id] = (normalized, now)
        return set(normalized)

    async def _increment_flagged_count(self, message: discord.Message) -> dict[str, int] | None:
        if message.guild is None:
            return None

        mongo_cog = self.bot.get_cog("MongoDbCog")
        if mongo_cog is None:
            return None

        try:
            return await mongo_cog.increment_user_moderation_flag_count(
                guild_id=message.guild.id,
                guild_name=message.guild.name,
                user_id=message.author.id,
                username=str(message.author),
            )
        except Exception as error:
            print(f"[WARN] Moderation: Failed to update user moderation count in MongoDB: {error}")
            return None

    async def _record_moderation_decision(
        self,
        guild: discord.Guild,
        source_user_id: int,
        approved: bool,
    ) -> dict[str, int] | None:
        mongo_cog = self.bot.get_cog("MongoDbCog")
        if mongo_cog is None:
            return None

        member = guild.get_member(source_user_id)
        username = str(member) if member is not None else str(source_user_id)

        try:
            return await mongo_cog.increment_user_moderation_decision_count(
                guild_id=guild.id,
                guild_name=guild.name,
                user_id=source_user_id,
                username=username,
                approved=approved,
            )
        except Exception as error:
            print(f"[WARN] Moderation: Failed to update moderation decision count in MongoDB: {error}")
            return None

    async def _record_moderation_enforcement(
        self,
        guild: discord.Guild,
        source_user_id: int,
        enforcement_action: str,
    ) -> dict[str, int] | None:
        mongo_cog = self.bot.get_cog("MongoDbCog")
        if mongo_cog is None:
            return None

        member = guild.get_member(source_user_id)
        username = str(member) if member is not None else str(source_user_id)

        try:
            return await mongo_cog.increment_user_moderation_enforcement_count(
                guild_id=guild.id,
                guild_name=guild.name,
                user_id=source_user_id,
                username=username,
                enforcement_action=enforcement_action,
            )
        except Exception as error:
            print(f"[WARN] Moderation: Failed to update moderation enforcement count in MongoDB: {error}")
            return None

    @staticmethod
    def _get_user_role_context(guild: discord.Guild, member: discord.Member) -> str | None:
        if guild.owner_id == member.id:
            return (
                "⚠️ This message was posted by the server owner. Kick/Ban actions are disabled. "
                "They should be spoken to directly as they are not setting a good example on the server."
            )

        if member.guild_permissions.administrator:
            return (
                "⚠️ This message was posted by an administrator. Kick/Ban actions are disabled. "
                "They should be spoken to directly as they are not setting a good example on the server."
            )

        return None

    async def mod_exclude_add(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only administrators can manage moderation exclusions.", ephemeral=True)
            return

        guild_exclusions = self.excluded_channel_ids_by_guild.setdefault(interaction.guild.id, set())
        if channel.id in guild_exclusions:
            await interaction.response.send_message(
                f"{channel.mention} is already excluded from moderation.",
                ephemeral=True,
            )
            return

        guild_exclusions.add(channel.id)
        self._save_excluded_channels()
        await interaction.response.send_message(
            f"✅ {channel.mention} is now excluded from moderation scanning.",
            ephemeral=True,
        )

    async def mod_exclude_remove(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only administrators can manage moderation exclusions.", ephemeral=True)
            return

        guild_exclusions = self.excluded_channel_ids_by_guild.get(interaction.guild.id)
        if not guild_exclusions or channel.id not in guild_exclusions:
            await interaction.response.send_message(
                f"{channel.mention} is not currently excluded.",
                ephemeral=True,
            )
            return

        guild_exclusions.remove(channel.id)
        if not guild_exclusions:
            self.excluded_channel_ids_by_guild.pop(interaction.guild.id, None)

        self._save_excluded_channels()
        await interaction.response.send_message(
            f"✅ {channel.mention} is no longer excluded from moderation scanning.",
            ephemeral=True,
        )

    async def mod_exclude_list(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only administrators can manage moderation exclusions.", ephemeral=True)
            return

        guild_exclusions = self._get_excluded_channels(interaction.guild.id)
        if not guild_exclusions:
            await interaction.response.send_message("No channels are currently excluded from moderation.", ephemeral=True)
            return

        mentions = []
        for channel_id in sorted(guild_exclusions):
            channel_obj = interaction.guild.get_channel(channel_id)
            if channel_obj and isinstance(channel_obj, discord.TextChannel):
                mentions.append(channel_obj.mention)
            else:
                mentions.append(f"<#{channel_id}> (unavailable)")

        await interaction.response.send_message(
            "Excluded channels:\n" + "\n".join(f"- {mention}" for mention in mentions),
            ephemeral=True,
        )

    async def mod_word_add(self, interaction: discord.Interaction, word: str):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only administrators can manage custom moderation words.", ephemeral=True)
            return

        normalized_word = self._normalize_custom_term(word)
        if normalized_word is None:
            await interaction.response.send_message(
                "Please provide a valid word/phrase between 2 and 64 characters.",
                ephemeral=True,
            )
            return

        mongo_cog = self.bot.get_cog("MongoDbCog")
        if mongo_cog is None:
            await interaction.response.send_message("MongoDB is unavailable. Try again shortly.", ephemeral=True)
            return

        try:
            was_added, terms, stored_word = await mongo_cog.add_guild_custom_moderation_term(
                guild_id=interaction.guild.id,
                guild_name=interaction.guild.name,
                term_raw=normalized_word,
            )
        except Exception as error:
            await interaction.response.send_message(
                f"Failed to add moderation word due to database error: {error}",
                ephemeral=True,
            )
            return

        if stored_word is None:
            await interaction.response.send_message(
                "Could not save that word. Please try again with a different value.",
                ephemeral=True,
            )
            return

        self.custom_terms_cache_by_guild[interaction.guild.id] = (set(terms), datetime.now(timezone.utc))

        if not was_added:
            await interaction.response.send_message(
                f"`{stored_word}` is already in the custom moderation word list.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"✅ Added `{stored_word}` to custom moderation words. Total custom words: {len(terms)}.",
            ephemeral=True,
        )

    async def mod_word_remove(self, interaction: discord.Interaction, word: str):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only administrators can manage custom moderation words.", ephemeral=True)
            return

        normalized_word = self._normalize_custom_term(word)
        if normalized_word is None:
            await interaction.response.send_message(
                "Please provide a valid word/phrase between 2 and 64 characters.",
                ephemeral=True,
            )
            return

        mongo_cog = self.bot.get_cog("MongoDbCog")
        if mongo_cog is None:
            await interaction.response.send_message("MongoDB is unavailable. Try again shortly.", ephemeral=True)
            return

        try:
            removed, terms, stored_word = await mongo_cog.remove_guild_custom_moderation_term(
                guild_id=interaction.guild.id,
                term_raw=normalized_word,
            )
        except Exception as error:
            await interaction.response.send_message(
                f"Failed to remove moderation word due to database error: {error}",
                ephemeral=True,
            )
            return

        if stored_word is None:
            await interaction.response.send_message(
                "Could not remove that word. Please try again with a different value.",
                ephemeral=True,
            )
            return

        self.custom_terms_cache_by_guild[interaction.guild.id] = (set(terms), datetime.now(timezone.utc))

        if not removed:
            await interaction.response.send_message(
                f"`{stored_word}` is not currently in the custom moderation word list.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"✅ Removed `{stored_word}` from custom moderation words. Total custom words: {len(terms)}.",
            ephemeral=True,
        )

    async def mod_word_list(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only administrators can manage custom moderation words.", ephemeral=True)
            return

        terms = await self._get_custom_terms_for_guild(interaction.guild.id)
        if not terms:
            await interaction.response.send_message("No custom moderation words are configured yet.", ephemeral=True)
            return

        lines = "\n".join(f"- `{term}`" for term in sorted(terms))
        await interaction.response.send_message(
            f"Custom moderation words ({len(terms)}):\n{lines}",
            ephemeral=True,
        )

    async def _ensure_admin_moderation_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        if not guild.me or not guild.me.guild_permissions.manage_channels:
            print(
                f"[WARN] Moderation: Cannot ensure admin channel in guild {guild.name} ({guild.id}) because Manage Channels is missing."
            )
            return None

        channel_settings = await self._get_moderation_channel_settings()
        channel_name = channel_settings["channel_name"]
        category_name = channel_settings["category_name"]
        channel_description = channel_settings["channel_description"]

        bot_member = guild.me

        category = await self._ensure_admin_category(guild)
        if category is None:
            return None

        overwrites = self._build_admin_moderation_overwrites(guild)

        existing = discord.utils.get(guild.text_channels, name=channel_name)
        if existing:
            if existing.category_id != category.id:
                try:
                    await existing.edit(
                        category=category,
                        sync_permissions=False,
                        overwrites=overwrites,
                        reason="Move moderation channel under DiscoBot admin category",
                    )
                except (discord.Forbidden, discord.HTTPException) as error:
                    print(
                        f"[WARN] Moderation: Failed moving {channel_name} under {category_name} "
                        f"in guild {guild.name} ({guild.id}): {error}"
                    )
                    if "Missing Access" in str(error):
                        self._log_manual_access_recovery(guild.id)
                    return None

            if not existing.permissions_for(bot_member).view_channel:
                try:
                    await existing.edit(
                        sync_permissions=False,
                        overwrites=overwrites,
                        reason="Ensure bot can access moderation channel",
                    )
                except (discord.Forbidden, discord.HTTPException) as error:
                    print(
                        f"[WARN] Moderation: Failed syncing permissions on {channel_name} "
                        f"in guild {guild.name} ({guild.id}): {error}"
                    )
                    if "Missing Access" in str(error):
                        self._log_manual_access_recovery(guild.id)

            if existing.topic != channel_description:
                try:
                    await existing.edit(
                        topic=channel_description,
                        overwrites=overwrites,
                        sync_permissions=False,
                        reason="Update moderation channel description",
                    )
                except (discord.Forbidden, discord.HTTPException) as error:
                    print(
                        f"[WARN] Moderation: Failed updating topic for {channel_name} "
                        f"in guild {guild.name} ({guild.id}): {error}"
                    )
                    if "Missing Access" in str(error):
                        self._log_manual_access_recovery(guild.id)
            return existing

        try:
            return await guild.create_text_channel(
                channel_name,
                category=category,
                overwrites=overwrites,
                topic=channel_description,
                reason="Setup moderation channel under DiscoBot admin category",
            )
        except (discord.Forbidden, discord.HTTPException) as error:
            print(
                f"[WARN] Moderation: Failed creating {channel_name} under {category_name} "
                f"in guild {guild.name} ({guild.id}): {error}"
            )
            if "Missing Access" in str(error):
                self._log_manual_access_recovery(guild.id)
            return None

    async def _ensure_admin_category(self, guild: discord.Guild) -> discord.CategoryChannel | None:
        if not guild.me or not guild.me.guild_permissions.manage_channels:
            return None

        channel_settings = await self._get_moderation_channel_settings()
        category_name = channel_settings["category_name"]

        bot_member = guild.me
        bot_role = bot_member.top_role if bot_member.top_role != guild.default_role else None
        admin_overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            bot_member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }

        if bot_role is not None:
            admin_overwrites[bot_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            )

        category = discord.utils.get(guild.categories, name=category_name)
        if category is None:
            try:
                return await guild.create_category(
                    category_name,
                    overwrites=admin_overwrites,
                    reason="Create DiscoBot admin-only category",
                )
            except (discord.Forbidden, discord.HTTPException) as error:
                print(
                    f"[WARN] Moderation: Failed creating category {category_name} "
                    f"in guild {guild.name} ({guild.id}): {error}"
                )
                if "Missing Access" in str(error):
                    self._log_manual_access_recovery(guild.id)
                return None

        if not category.permissions_for(bot_member).view_channel:
            print(
                f"[WARN] Moderation: Existing category {category_name} in guild {guild.name} ({guild.id}) "
                "does not grant bot view access."
            )
            self._log_manual_access_recovery(guild.id)

        return category

    def _build_admin_moderation_overwrites(
        self,
        guild: discord.Guild,
    ) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
        bot_member = guild.me
        if bot_member is None:
            return {
                guild.default_role: discord.PermissionOverwrite(view_channel=False, send_messages=False),
            }

        return {
            guild.default_role: discord.PermissionOverwrite(view_channel=False, send_messages=False),
            bot_member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            ),
        }

    def _detect_moderation_reasons(self, content: str, custom_terms: set[str] | None = None) -> list[str]:
        reasons: list[str] = []
        content_lower = content.lower()
        terms_to_scan = set(self.offensive_terms)
        if custom_terms:
            terms_to_scan.update(custom_terms)

        for term in terms_to_scan:
            if term in content_lower:
                reasons.append(f"Contains potentially rude phrase: '{term}'")

        for pattern in self.offensive_patterns:
            if pattern.search(content):
                reasons.append(f"Contains potentially offensive wording pattern: '{pattern.pattern}'")

        unique_reasons: list[str] = []
        seen = set()
        for reason in reasons:
            if reason not in seen:
                seen.add(reason)
                unique_reasons.append(reason)

        return unique_reasons

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            self._log_missing_permissions(guild)
            await self._ensure_admin_moderation_channel(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        self._log_missing_permissions(guild)
        await self._ensure_admin_moderation_channel(guild)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        if message.guild is None or not isinstance(message.channel, discord.TextChannel):
            return

        channel_settings = await self._get_moderation_channel_settings()
        if message.channel.name == channel_settings["channel_name"]:
            return

        if message.channel.id in self._get_excluded_channels(message.guild.id):
            return

        if not message.content or not message.content.strip():
            return

        custom_terms = await self._get_custom_terms_for_guild(message.guild.id)
        reasons = self._detect_moderation_reasons(message.content, custom_terms)
        if not reasons:
            return

        moderation_stats = await self._increment_flagged_count(message)

        mod_channel = await self._ensure_admin_moderation_channel(message.guild)
        if mod_channel is None:
            return

        excerpt = message.content
        if len(excerpt) > 900:
            excerpt = excerpt[:900] + "..."

        role_context = self._get_user_role_context(message.guild, message.author)
        allow_enforcement = role_context is None

        embed = discord.Embed(
            title="Potentially Offensive/Rude Message",
            description=excerpt,
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Author", value=f"{message.author} ({message.author.id})", inline=False)
        embed.add_field(
            name="Flagged Count",
            value=str(moderation_stats["moderation_flag_count"]) if moderation_stats is not None else "Unavailable",
            inline=True,
        )
        embed.add_field(
            name="Approved Count",
            value=str(moderation_stats["moderation_approved_count"]) if moderation_stats is not None else "Unavailable",
            inline=True,
        )
        embed.add_field(
            name="Rejected Count",
            value=str(moderation_stats["moderation_rejected_count"]) if moderation_stats is not None else "Unavailable",
            inline=True,
        )
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        embed.add_field(name="Original Message", value=f"[Jump to message]({message.jump_url})", inline=True)
        embed.add_field(name="Source User ID", value=str(message.author.id), inline=True)
        embed.add_field(name="Source Channel ID", value=str(message.channel.id), inline=True)
        embed.add_field(name="Source Message ID", value=str(message.id), inline=True)
        if role_context is not None:
            embed.add_field(name="Role Context", value=role_context, inline=False)
        embed.add_field(name="Reason", value="\n".join(f"- {reason}" for reason in reasons), inline=False)

        view = ModerationActionView(self, allow_enforcement=allow_enforcement)
        await mod_channel.send(embed=embed, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(ModerationCog(bot))
