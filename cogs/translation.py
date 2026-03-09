import os

import deepl
import discord
from discord import app_commands
from discord.ext import commands

from .translation_data import load_translation_data


class TranslationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.debug = os.environ.get("DEBUG", "false").lower() == "true"

        deepl_auth_key = os.environ["DEEPL_AUTH_KEY"]
        self.deepl_client = deepl.DeepLClient(deepl_auth_key)

        data = load_translation_data()
        self.discord_languages = data.discord_languages
        self.flag_emojis = data.flag_emojis
        self.discord_flag_languages = data.discord_flag_languages
        self.deepl_target_languages = data.deepl_target_languages

        self.translate_private_context_menu = app_commands.ContextMenu(
            name="Translate (Private)",
            callback=self.translate_private,
        )
        self.translate_context_menu = app_commands.ContextMenu(
            name="Translate",
            callback=self.translate_message,
        )

    async def cog_load(self):
        self.bot.tree.add_command(self.translate_private_context_menu)
        self.bot.tree.add_command(self.translate_context_menu)

    async def cog_unload(self):
        self.bot.tree.remove_command(
            self.translate_private_context_menu.name,
            type=self.translate_private_context_menu.type,
        )
        self.bot.tree.remove_command(
            self.translate_context_menu.name,
            type=self.translate_context_menu.type,
        )

    @staticmethod
    def is_empty(value: str | None):
        return value is None or len(value.strip()) == 0

    async def _is_translation_enabled(self, guild_id: int | None) -> bool:
        if guild_id is None:
            return True

        mongo_cog = self.bot.get_cog("MongoDbCog")
        if mongo_cog is None:
            return True

        try:
            features = await mongo_cog.get_guild_features(guild_id)
            translation_feature = features.get("translation", {}) if isinstance(features, dict) else {}
            return bool(translation_feature.get("enabled", True)) if isinstance(translation_feature, dict) else True
        except Exception as error:
            if self.debug:
                print(f"[DEBUG] Error checking translation feature flag for guild {guild_id}: {error}")
            return True

    async def _debug_log_character_usage(self, guild_id: int | None, character_count: int):
        if not self.debug or guild_id is None:
            return

        mongo_cog = self.bot.get_cog("MongoDbCog")
        if not mongo_cog:
            print(
                f"[DEBUG] Translation character usage: used {character_count} characters; guild allowance unavailable (MongoDbCog not loaded)."
            )
            return

        try:
            _, current_count, allowance = await mongo_cog.check_translation_character_limit(guild_id)
            remaining = max(allowance - current_count, 0)
            print(
                f"[DEBUG] Translation character usage: used {character_count} characters; total {current_count}/{allowance}; remaining {remaining}."
            )
        except Exception as error:
            print(f"[DEBUG] Error retrieving translation character usage for guild {guild_id}: {error}")

    @staticmethod
    def _format_character_budget_message(budget: dict) -> str:
        guild_used = int(budget.get("guild_used", 0) or 0)
        guild_allowance = int(budget.get("guild_allowance", 0) or 0)
        personal_used = int(budget.get("personal_used", 0) or 0)
        personal_allowance = int(budget.get("personal_allowance", 0) or 0)

        if personal_allowance > 0:
            return (
                "⚠️ Translation character limit exceeded! "
                f"Guild usage is {guild_used}/{guild_allowance} and your personal translation usage is "
                f"{personal_used}/{personal_allowance} characters this month."
            )

        return (
            "⚠️ Translation character limit exceeded! "
            f"This server has used {guild_used}/{guild_allowance} characters this month."
        )

    @staticmethod
    def _format_personal_character_budget_message(budget: dict) -> str:
        personal_used = int(budget.get("personal_used", 0) or 0)
        personal_allowance = int(budget.get("personal_allowance", 0) or 0)
        return (
            "⚠️ Personal translation character limit exceeded! "
            f"You have used {personal_used}/{personal_allowance} characters this month."
        )

    async def _send_private_limit_notice(
        self,
        user_id: int,
        fallback_channel,
        message: str,
    ):
        # Raw reaction events do not support ephemeral responses, so prefer DM for private notices.
        try:
            user = self.bot.get_user(user_id)
            if user is None:
                user = await self.bot.fetch_user(user_id)
            await user.send(message)
            return
        except Exception as error:
            if self.debug:
                print(f"[DEBUG] Could not DM private limit notice to user {user_id}: {error}")

        # Fallback only when DM cannot be delivered.
        await fallback_channel.send(message, delete_after=10)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if self.debug:
            print(
                f"[DEBUG] Received reaction: {payload.emoji} from user ID {payload.user_id} on message ID {payload.message_id} in channel ID {payload.channel_id}"
            )

        if self.bot.user and payload.user_id == self.bot.user.id:
            return

        if payload.guild_id and not await self._is_translation_enabled(payload.guild_id):
            return

        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(payload.channel_id)
            except Exception as e:
                if self.debug:
                    print(f"[DEBUG] Could not resolve channel {payload.channel_id} for reaction translation: {e}")
                return

        # Check translation limit if this is in a guild
        if payload.guild_id:
            mongo_cog = self.bot.get_cog("MongoDbCog")
            if mongo_cog:
                try:
                    can_translate, current_count, allowance = await mongo_cog.check_translation_limit(payload.guild_id)
                    if not can_translate:
                        await self._send_private_limit_notice(
                            payload.user_id,
                            channel,
                            f"⚠️ Translation limit exceeded! This server has used {current_count}/{allowance} translations this month."
                        )
                        return
                except Exception as e:
                    if self.debug:
                        print(f"[DEBUG] Error checking translation limit: {e}")
                    # Continue with translation if we can't check the limit

        message = await channel.fetch_message(payload.message_id)
        member = payload.member
        if member is None and payload.guild_id:
            guild = self.bot.get_guild(payload.guild_id)
            if guild is not None:
                member = guild.get_member(payload.user_id)
        requester_name = member.display_name if member is not None else "Unknown"
        if requester_name == "Unknown":
            requester_user = self.bot.get_user(payload.user_id)
            if requester_user is not None:
                requester_name = requester_user.display_name

        target_deepl_locale = ""
        target_language_name = "Unknown"
        target_native_name = "Unknown"
        target_flag_emoji = ""

        if str(payload.emoji) not in self.flag_emojis:
            if self.debug:
                print(f"[DEBUG] Emoji [{payload.emoji}] is not a supported flag emoji. Skipping translation.")
            return

        if str(payload.emoji) == "🏴‍☠️":
            await channel.send(
                "🏴‍☠️ Arrrrr, shivvver me timbers! Translatin' to pirate ain't supported, but ye can try selectin' a different flag, matey!"
            )
            return

        if str(payload.emoji) == "🏳️‍🌈":
            await channel.send("🏳️‍🌈 I can't translate to pride. Just be bold, loud, and proud...")
            return

        for language in self.discord_flag_languages:
            if language.get("flagEmoji") == str(payload.emoji) or language.get("discord_flag_emoji") == str(payload.emoji):
                target_deepl_locale = language.get("deepLlocale")
                target_language_name = language.get("languageName")
                target_native_name = language.get("nativeName")
                target_flag_emoji = language.get("flagEmoji")

        if self.is_empty(target_deepl_locale):
            await channel.send(f"Sorry, the {payload.emoji.name} flag is not currently supported for translation.")
            if self.debug:
                print(f"[DEBUG] Emoji [{payload.emoji}] does not correspond to a supported DeepL locale. Skipping translation.")
            return

        if self.debug:
            print(f"[DEBUG] Flag {payload.emoji} corresponds to DeepL locale {target_deepl_locale}. Proceeding...")

        deep_l_language_found = False
        for check_deepl_language in self.deepl_target_languages:
            if check_deepl_language.get("language") == target_deepl_locale:
                deep_l_language_found = True
                if self.debug:
                    print(f"[DEBUG] DeepL supports the target language {target_deepl_locale}. Continuing with translation.")
                break

        if not deep_l_language_found:
            await channel.send(f"Sorry, the {payload.emoji.name} flag is not currently supported for DeepL translation.")
            if self.debug:
                print(f"[DEBUG] DeepL does not support the target language {target_deepl_locale}. Skipping translation.")
            return

        if message.author == self.bot.user:
            try:
                result = self.deepl_client.translate_text(
                    text="You can't translate this. Please select an original user message",
                    target_lang=target_deepl_locale,
                )
                await channel.send(result.text)
            except Exception as e:
                if self.debug:
                    print(f"[DEBUG] Error translating skip message: {str(e)}")
                await channel.send(f"Oops, There has been an error: {str(e)}")
            return

        if message.content is None or message.content.strip() == "":
            try:
                result = self.deepl_client.translate_text(
                    text="This message appears to be empty. Please try again with a valid message.",
                    target_lang=target_deepl_locale,
                )
                await channel.send(result.text)
            except Exception as e:
                await channel.send(f"Oops, There has been an error: {str(e)}")
            return

        # Check translation character budget (guild pool first, then personal pool)
        character_budget_source = "guild"
        if payload.guild_id:
            mongo_cog = self.bot.get_cog("MongoDbCog")
            if mongo_cog:
                try:
                    budget = await mongo_cog.check_translation_character_budget(
                        payload.guild_id,
                        payload.user_id,
                        len(message.content),
                    )
                    if not budget.get("can_translate", False):
                        await self._send_private_limit_notice(
                            payload.user_id,
                            channel,
                            self._format_character_budget_message(budget),
                        )
                        return
                    character_budget_source = str(budget.get("source") or "guild")
                except Exception as e:
                    if self.debug:
                        print(f"[DEBUG] Error checking translation character budget: {e}")
                    # Continue with translation if we can't check the limit
        else:
            mongo_cog = self.bot.get_cog("MongoDbCog")
            if mongo_cog:
                try:
                    budget = await mongo_cog.check_personal_translation_character_budget(
                        payload.user_id,
                        len(message.content),
                    )
                    if not budget.get("can_translate", False):
                        await self._send_private_limit_notice(
                            payload.user_id,
                            channel,
                            self._format_personal_character_budget_message(budget),
                        )
                        return
                    character_budget_source = "personal"
                except Exception as e:
                    if self.debug:
                        print(f"[DEBUG] Error checking personal translation character budget: {e}")
                    # Continue with translation if we can't check the limit

        try:
            result = self.deepl_client.translate_text(text=message.content, target_lang=target_deepl_locale)
            source_locale = result.detected_source_lang

            source_language_name = "Unknown"
            source_native_name = "Unknown"
            source_flag_emoji = "🏳️"

            if source_locale == "EN":
                source_locale = "EN-US"
                source_language_name = "English (US)"
                source_native_name = "English (US)"
                source_flag_emoji = "🇺🇸"

            if source_locale == "ZH":
                source_locale = "ZH-CN"
                source_language_name = "Chinese, China"
                source_native_name = "中文"
                source_flag_emoji = "🇨🇳"

            for language in self.discord_flag_languages:
                if language.get("deepLlocale") == source_locale:
                    source_language_name = language.get("languageName")
                    source_native_name = language.get("nativeName")
                    source_flag_emoji = language.get("flagEmoji")

            translated_text = result.text
            source_language = result.detected_source_lang

            embed = discord.Embed(description=f"> {translated_text}", color=discord.Color.light_gray())
            embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)

            if member is not None:
                embed.set_footer(
                    text=f"{member.display_name} translated using Flag Reaction feature\n{source_flag_emoji} {source_language} to {target_flag_emoji} {target_deepl_locale}",
                    icon_url=member.display_avatar.url,
                )
            else:
                embed.set_footer(
                    text=f"Translated using Flag Reaction feature\n{source_flag_emoji} {source_language} to {target_flag_emoji} {target_deepl_locale}"
                )

            try:
                await channel.send(reference=message, content="", embed=embed)
            except Exception:
                # DM and some channel types can reject message references; fall back to a normal send.
                await channel.send(content="", embed=embed)
            
            # Consume translation usage
            if payload.guild_id:
                mongo_cog = self.bot.get_cog("MongoDbCog")
                if mongo_cog:
                    try:
                        usage_result = await mongo_cog.consume_translation_character_usage(
                            payload.guild_id,
                            payload.user_id,
                            requester_name,
                            len(message.content),
                            source_hint=character_budget_source,
                        )
                        if self.debug:
                            print(
                                "[DEBUG] Translation usage consumed "
                                f"source={usage_result.get('source')} "
                                f"guild={usage_result.get('guild_used')}/{usage_result.get('guild_allowance')} "
                                f"personal={usage_result.get('personal_used')}/{usage_result.get('personal_allowance')}"
                            )
                    except Exception as e:
                        if self.debug:
                            print(f"[DEBUG] Error consuming translation usage: {e}")
            else:
                mongo_cog = self.bot.get_cog("MongoDbCog")
                if mongo_cog:
                    try:
                        usage_result = await mongo_cog.consume_personal_translation_character_usage(
                            payload.user_id,
                            requester_name,
                            len(message.content),
                        )
                        if self.debug:
                            print(
                                "[DEBUG] Personal translation usage consumed "
                                f"personal={usage_result.get('personal_used')}/{usage_result.get('personal_allowance')}"
                            )
                    except Exception as e:
                        if self.debug:
                            print(f"[DEBUG] Error consuming personal translation usage: {e}")
        except Exception as e:
            await channel.send(f"Oops, There has been an error: {str(e)}")

    async def translate_message(self, interaction: discord.Interaction, message: discord.Message):
        guild_name = interaction.guild.name if interaction.guild else "DM"
        print(f"Running translate_message for user {interaction.user.name} in guild {guild_name}")

        if interaction.guild_id and not await self._is_translation_enabled(interaction.guild_id):
            await interaction.response.send_message(
                "What a drag, some fool has disabled my awesome 'Translation' feature 😢\n\nWhy not bug your admins to get it enabled again? 😏🤣",
                ephemeral=True,
            )
            return

        # Check translation limit
        if interaction.guild_id:
            mongo_cog = self.bot.get_cog("MongoDbCog")
            if mongo_cog:
                try:
                    can_translate, current_count, allowance = await mongo_cog.check_translation_limit(interaction.guild_id)
                    if not can_translate:
                        await interaction.response.send_message(
                            f"⚠️ Translation limit exceeded! This server has used {current_count}/{allowance} translations this month.",
                            ephemeral=True
                        )
                        return
                except Exception as e:
                    if self.debug:
                        print(f"[DEBUG] Error checking translation limit: {e}")
                    # Continue with translation if we can't check the limit

        target_locale = str(interaction.locale)
        target_deepl_locale = "Unknown"
        target_language_name = "Unknown"
        target_native_name = "Unknown"
        target_flag_emoji = "🇺🇸"

        for language in self.discord_languages:
            if language.get("locale") == target_locale:
                target_deepl_locale = language.get("deepLlocale")
                target_language_name = language.get("languageName")
                target_native_name = language.get("nativeName")
                target_flag_emoji = language.get("flagEmoji")

        if self.is_empty(target_deepl_locale):
            target_deepl_locale = "EN-US"
            target_language_name = "English (US)"
            target_native_name = "English (US)"
            target_flag_emoji = "🇺🇸"

        if target_language_name != target_native_name:
            target_language_display = f"{target_language_name} ({target_native_name})"
        else:
            target_language_display = target_language_name

        if message.author == self.bot.user:
            try:
                result = self.deepl_client.translate_text(
                    text="You can't translate this. Please select an original user message",
                    target_lang=target_deepl_locale,
                )
                await interaction.response.send_message(result.text, ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"Oops, There has been an error: {str(e)}", ephemeral=True)
            return

        if message.content is None or message.content.strip() == "":
            try:
                result = self.deepl_client.translate_text(
                    text="This message appears to be empty. Please try again with a valid message.",
                    target_lang=target_deepl_locale,
                )
                await interaction.response.send_message(result.text, ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"Oops, There has been an error: {str(e)}", ephemeral=True)
            return

        # Check translation character budget (guild pool first, then personal pool)
        character_budget_source = "guild"
        if interaction.guild_id:
            mongo_cog = self.bot.get_cog("MongoDbCog")
            if mongo_cog:
                try:
                    budget = await mongo_cog.check_translation_character_budget(
                        interaction.guild_id,
                        interaction.user.id,
                        len(message.content),
                    )
                    if not budget.get("can_translate", False):
                        await interaction.response.send_message(self._format_character_budget_message(budget), ephemeral=True)
                        return
                    character_budget_source = str(budget.get("source") or "guild")
                except Exception as e:
                    if self.debug:
                        print(f"[DEBUG] Error checking translation character budget: {e}")
                    # Continue with translation if we can't check the limit
        else:
            mongo_cog = self.bot.get_cog("MongoDbCog")
            if mongo_cog:
                try:
                    budget = await mongo_cog.check_personal_translation_character_budget(
                        interaction.user.id,
                        len(message.content),
                    )
                    if not budget.get("can_translate", False):
                        await interaction.response.send_message(self._format_personal_character_budget_message(budget), ephemeral=True)
                        return
                    character_budget_source = "personal"
                except Exception as e:
                    if self.debug:
                        print(f"[DEBUG] Error checking personal translation character budget: {e}")
                    # Continue with translation if we can't check the limit

        try:
            result = self.deepl_client.translate_text(text=message.content, target_lang=target_deepl_locale)
            source_locale = result.detected_source_lang

            source_language_name = "Unknown"
            source_native_name = "Unknown"
            source_flag_emoji = "🏳️"

            if source_locale == "EN":
                source_locale = "EN-US"
                source_language_name = "English (US)"
                source_native_name = "English (US)"
                source_flag_emoji = "🇺🇸"

            if source_locale == "ZH":
                source_locale = "ZH-CN"
                source_language_name = "Chinese, China"
                source_native_name = "中文"
                source_flag_emoji = "🇨🇳"

            for language in self.discord_languages:
                if language.get("deepLlocale") == source_locale:
                    source_language_name = language.get("languageName")
                    source_native_name = language.get("nativeName")
                    source_flag_emoji = language.get("flagEmoji")

            if source_language_name != source_native_name:
                source_language_display = f"{source_language_name} ({source_native_name})"
            else:
                source_language_display = source_language_name

            original_message_text = self.deepl_client.translate_text(
                text="Original Message", target_lang=target_deepl_locale
            )
            translated_message_text = self.deepl_client.translate_text(
                text="Translated Message", target_lang=target_deepl_locale
            )

            translated_text = result.text
            embed = discord.Embed(description="", color=discord.Color.light_gray())
            embed.add_field(name=original_message_text.text, value=message.content, inline=True)
            embed.set_footer(text=f"{source_flag_emoji} {source_locale} - {source_language_display}")
            embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)

            embed2 = discord.Embed(description="", color=discord.Color.green())
            embed2.add_field(name=translated_message_text.text, value=translated_text, inline=False)
            embed2.set_footer(text=f"{target_flag_emoji} {target_deepl_locale} - {target_language_display}")

            await interaction.response.send_message(embeds=[embed, embed2])
            
            # Consume translation usage
            if interaction.guild_id:
                mongo_cog = self.bot.get_cog("MongoDbCog")
                if mongo_cog:
                    try:
                        usage_result = await mongo_cog.consume_translation_character_usage(
                            interaction.guild_id,
                            interaction.user.id,
                            interaction.user.display_name,
                            len(message.content),
                            source_hint=character_budget_source,
                        )
                        if self.debug:
                            print(
                                "[DEBUG] Translation usage consumed "
                                f"source={usage_result.get('source')} "
                                f"guild={usage_result.get('guild_used')}/{usage_result.get('guild_allowance')} "
                                f"personal={usage_result.get('personal_used')}/{usage_result.get('personal_allowance')}"
                            )
                    except Exception as e:
                        if self.debug:
                            print(f"[DEBUG] Error consuming translation usage: {e}")
            else:
                mongo_cog = self.bot.get_cog("MongoDbCog")
                if mongo_cog:
                    try:
                        usage_result = await mongo_cog.consume_personal_translation_character_usage(
                            interaction.user.id,
                            interaction.user.display_name,
                            len(message.content),
                        )
                        if self.debug:
                            print(
                                "[DEBUG] Personal translation usage consumed "
                                f"personal={usage_result.get('personal_used')}/{usage_result.get('personal_allowance')}"
                            )
                    except Exception as e:
                        if self.debug:
                            print(f"[DEBUG] Error consuming personal translation usage: {e}")
        except Exception as e:
            await interaction.response.send_message(f"Oops, There has been an error: {str(e)}", ephemeral=True)

    async def translate_private(self, interaction: discord.Interaction, message: discord.Message):
        guild_name = interaction.guild.name if interaction.guild else "DM"
        print(f"Running translate_private for user {interaction.user.name} in guild {guild_name}")

        if interaction.guild_id and not await self._is_translation_enabled(interaction.guild_id):
            await interaction.response.send_message(
                "What a drag, some fool has disabled my awesome 'Translation' feature 😢\n\nWhy not bug your admins to get it enabled again? 😏🤣",
                ephemeral=True,
            )
            return

        # Check translation limit
        if interaction.guild_id:
            mongo_cog = self.bot.get_cog("MongoDbCog")
            if mongo_cog:
                try:
                    can_translate, current_count, allowance = await mongo_cog.check_translation_limit(interaction.guild_id)
                    if not can_translate:
                        await interaction.response.send_message(
                            f"⚠️ Translation limit exceeded! This server has used {current_count}/{allowance} translations this month.",
                            ephemeral=True
                        )
                        return
                except Exception as e:
                    if self.debug:
                        print(f"[DEBUG] Error checking translation limit: {e}")
                    # Continue with translation if we can't check the limit

        target_locale = str(interaction.locale)
        target_deepl_locale = "Unknown"
        target_language_name = "Unknown"
        target_native_name = "Unknown"
        target_flag_emoji = "🇺🇸"

        for language in self.discord_languages:
            if language.get("locale") == target_locale:
                target_deepl_locale = language.get("deepLlocale")
                target_language_name = language.get("languageName")
                target_native_name = language.get("nativeName")
                target_flag_emoji = language.get("flagEmoji")

        if self.is_empty(target_deepl_locale):
            target_deepl_locale = "EN-US"
            target_language_name = "English (US)"
            target_native_name = "English (US)"
            target_flag_emoji = "🇺🇸"

        if target_language_name != target_native_name:
            target_language_display = f"{target_language_name} ({target_native_name})"
        else:
            target_language_display = target_language_name

        if message.author == self.bot.user:
            try:
                result = self.deepl_client.translate_text(
                    text="You can't translate this. Please select an original user message",
                    target_lang=target_deepl_locale,
                )
                await interaction.response.send_message(result.text, ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"Oops, There has been an error: {str(e)}", ephemeral=True)
            return

        if message.content is None or message.content.strip() == "":
            try:
                result = self.deepl_client.translate_text(
                    text="This message appears to be empty. Please try again with a valid message.",
                    target_lang=target_deepl_locale,
                )
                await interaction.response.send_message(result.text, ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"Oops, There has been an error: {str(e)}", ephemeral=True)
            return

        # Check translation character budget (guild pool first, then personal pool)
        character_budget_source = "guild"
        if interaction.guild_id:
            mongo_cog = self.bot.get_cog("MongoDbCog")
            if mongo_cog:
                try:
                    budget = await mongo_cog.check_translation_character_budget(
                        interaction.guild_id,
                        interaction.user.id,
                        len(message.content),
                    )
                    if not budget.get("can_translate", False):
                        await interaction.response.send_message(self._format_character_budget_message(budget), ephemeral=True)
                        return
                    character_budget_source = str(budget.get("source") or "guild")
                except Exception as e:
                    if self.debug:
                        print(f"[DEBUG] Error checking translation character budget: {e}")
                    # Continue with translation if we can't check the limit
        else:
            mongo_cog = self.bot.get_cog("MongoDbCog")
            if mongo_cog:
                try:
                    budget = await mongo_cog.check_personal_translation_character_budget(
                        interaction.user.id,
                        len(message.content),
                    )
                    if not budget.get("can_translate", False):
                        await interaction.response.send_message(self._format_personal_character_budget_message(budget), ephemeral=True)
                        return
                    character_budget_source = "personal"
                except Exception as e:
                    if self.debug:
                        print(f"[DEBUG] Error checking personal translation character budget: {e}")
                    # Continue with translation if we can't check the limit

        try:
            result = self.deepl_client.translate_text(text=message.content, target_lang=target_deepl_locale)
            source_locale = result.detected_source_lang

            source_language_name = "Unknown"
            source_native_name = "Unknown"
            source_flag_emoji = "🏳️"

            if source_locale == "EN":
                source_locale = "EN-US"
                source_language_name = "English (US)"
                source_native_name = "English (US)"
                source_flag_emoji = "🇺🇸"

            if source_locale == "ZH":
                source_locale = "ZH-CN"
                source_language_name = "Chinese, China"
                source_native_name = "中文"
                source_flag_emoji = "🇨🇳"

            for language in self.discord_languages:
                if language.get("deepLlocale") == source_locale:
                    source_language_name = language.get("languageName")
                    source_native_name = language.get("nativeName")
                    source_flag_emoji = language.get("flagEmoji")

            if source_language_name != source_native_name:
                source_language_display = f"{source_language_name} ({source_native_name})"
            else:
                source_language_display = source_language_name

            original_message_text = self.deepl_client.translate_text(
                text="Original Message", target_lang=target_deepl_locale
            )
            translated_message_text = self.deepl_client.translate_text(
                text="Translated Message", target_lang=target_deepl_locale
            )

            translated_text = result.text
            embed = discord.Embed(description="", color=discord.Color.red())
            embed.add_field(name=original_message_text.text, value=message.content, inline=False)
            embed.set_footer(text=f"{source_flag_emoji} {source_locale} - {source_language_display}")
            embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)

            embed2 = discord.Embed(description="", color=discord.Color.green())
            embed2.add_field(name=translated_message_text.text, value=translated_text, inline=False)
            embed2.set_footer(text=f"{target_flag_emoji} {target_deepl_locale} - {target_language_display}")

            await interaction.response.send_message(embeds=[embed, embed2], ephemeral=True)
            
            # Consume translation usage
            if interaction.guild_id:
                mongo_cog = self.bot.get_cog("MongoDbCog")
                if mongo_cog:
                    try:
                        usage_result = await mongo_cog.consume_translation_character_usage(
                            interaction.guild_id,
                            interaction.user.id,
                            interaction.user.display_name,
                            len(message.content),
                            source_hint=character_budget_source,
                        )
                        if self.debug:
                            print(
                                "[DEBUG] Translation usage consumed "
                                f"source={usage_result.get('source')} "
                                f"guild={usage_result.get('guild_used')}/{usage_result.get('guild_allowance')} "
                                f"personal={usage_result.get('personal_used')}/{usage_result.get('personal_allowance')}"
                            )
                    except Exception as e:
                        if self.debug:
                            print(f"[DEBUG] Error consuming translation usage: {e}")
            else:
                mongo_cog = self.bot.get_cog("MongoDbCog")
                if mongo_cog:
                    try:
                        usage_result = await mongo_cog.consume_personal_translation_character_usage(
                            interaction.user.id,
                            interaction.user.display_name,
                            len(message.content),
                        )
                        if self.debug:
                            print(
                                "[DEBUG] Personal translation usage consumed "
                                f"personal={usage_result.get('personal_used')}/{usage_result.get('personal_allowance')}"
                            )
                    except Exception as e:
                        if self.debug:
                            print(f"[DEBUG] Error consuming personal translation usage: {e}")
        except Exception as e:
            await interaction.response.send_message(f"Oops, There has been an error: {str(e)}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(TranslationCog(bot))
