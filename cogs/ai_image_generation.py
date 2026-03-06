import asyncio
import base64
import io
import os
import time

import discord
from discord import app_commands
from discord.ext import commands
from openai import OpenAI


class AiCreditsCallToActionView(discord.ui.View):
    def __init__(self, credits_url: str):
        super().__init__(timeout=None)
        self.credits_url = credits_url
        self.add_item(
            discord.ui.Button(
                label="Open website",
                style=discord.ButtonStyle.link,
                url=self.credits_url,
            )
        )

    @discord.ui.button(label="Buy AI credits", style=discord.ButtonStyle.success, emoji="💚")
    async def buy_ai_credits(self, interaction: discord.Interaction, _: discord.ui.Button):
        link_view = discord.ui.View()
        link_view.add_item(
            discord.ui.Button(
                label="Open credits page",
                url=self.credits_url,
            )
        )

        await interaction.response.send_message(
            f"Open the credits page here: {self.credits_url}",
            view=link_view,
            ephemeral=True,
        )



class AiImageGenerationCog(commands.Cog):
    DEFAULT_CATEGORY_NAME = "DiscoBot"
    DEFAULT_AI_IMAGE_CHANNEL_NAME = "ai-image-generation"
    DEFAULT_AI_IMAGE_CHANNEL_TOPIC = (
        "Generate AI images by right-clicking a message and selecting 'Apps > Generate AI Image'. "
        "Use text-only for new images, or include text + an attached image to generate from that image."
    )
    AI_IMAGE_CHANNEL_GUIDE_TITLE = "🎨 AI Image Generation Guide"
    AI_IMAGE_CHANNEL_GUIDE_TEXT = (
        "Use this channel for all AI image generation requests.\n\n"
        "**Option 1 — Generate from text**\n"
        "1. Send a message with the prompt text (for example: `A futuristic city at sunset`).\n"
        "2. Right-click your message → **Apps** → **Generate AI Image**.\n\n"
        "**Option 2 — Generate from an attached image + text**\n"
        "1. Send a message that includes both:\n"
        "   - an attached image\n"
        "   - text instructions describing how to transform it\n"
        "2. Right-click that message → **Apps** → **Generate AI Image**.\n\n"
        "Your generated image will be posted publicly here. Your credit details are sent privately (ephemeral)."
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.debug = os.environ.get("DEBUG", "false").lower() == "true"

        self.openai_api_key = os.environ.get("OPENAI_API_KEY")
        self.openai_client = OpenAI(api_key=self.openai_api_key) if self.openai_api_key else None
        self.website_base_url = os.environ.get("WEBSITE_BASE_URL", "http://localhost:3000").rstrip("/")

        self.generate_ai_image_context_menu = app_commands.ContextMenu(
            name="Generate AI Image",
            callback=self.generate_ai_image,
        )
        self._ai_channel_id_by_guild: dict[int, int] = {}
        self._channel_settings_cache: dict[str, str] | None = None
        self._channel_settings_loaded_at: float = 0.0

    async def _get_website_base_url(self) -> str:
        mongo_cog = self.bot.get_cog("MongoDbCog")
        if mongo_cog and hasattr(mongo_cog, "get_owner_website_base_url"):
            try:
                return await mongo_cog.get_owner_website_base_url(self.website_base_url)
            except Exception:
                return self.website_base_url

        return self.website_base_url

    async def _is_ai_image_enabled(self, guild_id: int | None) -> bool:
        if guild_id is None:
            return True

        mongo_cog = self.bot.get_cog("MongoDbCog")
        if mongo_cog is None:
            return True

        try:
            features = await mongo_cog.get_guild_features(guild_id)
            ai_feature = features.get("ai_image", {}) if isinstance(features, dict) else {}
            return bool(ai_feature.get("enabled", True)) if isinstance(ai_feature, dict) else True
        except Exception as error:
            if self.debug:
                print(f"[DEBUG] Error checking ai_image feature flag for guild {guild_id}: {error}")
            return True

    async def _get_ai_channel_settings(self) -> dict[str, str]:
        now = time.time()
        if self._channel_settings_cache is not None and (now - self._channel_settings_loaded_at) <= 120:
            return self._channel_settings_cache

        settings = {
            "category_name": self.DEFAULT_CATEGORY_NAME,
            "channel_name": self.DEFAULT_AI_IMAGE_CHANNEL_NAME,
            "channel_topic": self.DEFAULT_AI_IMAGE_CHANNEL_TOPIC,
        }

        mongo_cog = self.bot.get_cog("MongoDbCog")
        if mongo_cog is not None:
            try:
                owner_settings = await mongo_cog.get_bot_owner_settings()
                ai_settings = (
                    owner_settings.get("channels", {}).get("ai_image", {})
                    if isinstance(owner_settings, dict)
                    else {}
                )

                settings["category_name"] = str(ai_settings.get("category_name") or settings["category_name"])
                settings["channel_name"] = str(ai_settings.get("channel_name") or settings["channel_name"])
                settings["channel_topic"] = str(ai_settings.get("channel_topic") or settings["channel_topic"])
            except Exception as error:
                if self.debug:
                    print(f"[DEBUG] AI image settings fallback to defaults due to error: {error}")

        self._channel_settings_cache = settings
        self._channel_settings_loaded_at = now
        return settings

    async def cog_load(self):
        self.bot.tree.add_command(self.generate_ai_image_context_menu)

    async def cog_unload(self):
        self.bot.tree.remove_command(
            self.generate_ai_image_context_menu.name,
            type=self.generate_ai_image_context_menu.type,
        )

    async def _ensure_discobot_category(self, guild: discord.Guild) -> discord.CategoryChannel | None:
        channel_settings = await self._get_ai_channel_settings()
        category_name = channel_settings["category_name"]

        existing = discord.utils.get(guild.categories, name=category_name)
        if existing:
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

    async def _ensure_ai_image_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        if guild.me is None:
            return None

        channel_settings = await self._get_ai_channel_settings()
        channel_name = channel_settings["channel_name"]
        channel_topic = channel_settings["channel_topic"]

        existing = discord.utils.get(guild.text_channels, name=channel_name)
        category = await self._ensure_discobot_category(guild)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }

        if existing:
            needs_update = category is not None and existing.category_id != category.id
            if existing.topic != channel_topic and guild.me.guild_permissions.manage_channels:
                try:
                    await existing.edit(
                        topic=channel_topic,
                        reason="Set AI image generation channel instructions",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass

            if needs_update and guild.me.guild_permissions.manage_channels:
                try:
                    await existing.edit(
                        category=category,
                        sync_permissions=False,
                        overwrites=overwrites,
                        reason="Move AI image channel under DiscoBot category",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass

            self._ai_channel_id_by_guild[guild.id] = existing.id
            await self._ensure_ai_channel_guide_message(existing)
            return existing

        if not guild.me.guild_permissions.manage_channels:
            return None

        try:
            created = await guild.create_text_channel(
                channel_name,
                category=category,
                overwrites=overwrites,
                topic=channel_topic,
                reason="Create AI image generation channel for DiscoBot",
            )
            self._ai_channel_id_by_guild[guild.id] = created.id
            await self._ensure_ai_channel_guide_message(created)
            return created
        except (discord.Forbidden, discord.HTTPException):
            return None

    async def _ensure_ai_channel_guide_message(self, channel: discord.TextChannel):
        website_base_url = await self._get_website_base_url()
        buy_credits_url = f"{website_base_url}/credits"
        view = AiCreditsCallToActionView(buy_credits_url)

        try:
            async for message in channel.history(limit=25):
                if message.author.id != self.bot.user.id:
                    continue
                if not message.embeds:
                    continue
                if message.embeds[0].title != self.AI_IMAGE_CHANNEL_GUIDE_TITLE:
                    continue

                embed = message.embeds[0].copy()
                if embed.description != self.AI_IMAGE_CHANNEL_GUIDE_TEXT:
                    embed.description = self.AI_IMAGE_CHANNEL_GUIDE_TEXT
                await message.edit(embed=embed, view=view)

                if not message.pinned:
                    try:
                        await message.pin(reason="Pin AI image generation usage guide")
                    except (discord.Forbidden, discord.HTTPException):
                        pass
                return

            embed = discord.Embed(
                title=self.AI_IMAGE_CHANNEL_GUIDE_TITLE,
                description=self.AI_IMAGE_CHANNEL_GUIDE_TEXT,
                color=discord.Color.blurple(),
            )
            posted = await channel.send(embed=embed, view=view)
            try:
                await posted.pin(reason="Pin AI image generation usage guide")
            except (discord.Forbidden, discord.HTTPException):
                pass
        except (discord.Forbidden, discord.HTTPException):
            return

    async def _get_ai_image_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        cached_id = self._ai_channel_id_by_guild.get(guild.id)
        if cached_id:
            cached_channel = guild.get_channel(cached_id)
            if isinstance(cached_channel, discord.TextChannel):
                return cached_channel

        return await self._ensure_ai_image_channel(guild)

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            if await self._is_ai_image_enabled(guild.id):
                await self._ensure_ai_image_channel(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        if await self._is_ai_image_enabled(guild.id):
            await self._ensure_ai_image_channel(guild)

    @staticmethod
    def _get_image_attachment(message: discord.Message) -> discord.Attachment | None:
        for attachment in message.attachments:
            content_type = attachment.content_type or ""
            filename = attachment.filename.lower()
            if content_type.startswith("image/") or filename.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                return attachment
        return None

    @staticmethod
    def _extract_generated_image_bytes(response) -> bytes:
        if not getattr(response, "data", None):
            raise RuntimeError("OpenAI returned an empty image response.")

        image_data = response.data[0]
        image_b64 = getattr(image_data, "b64_json", None)
        if not image_b64:
            raise RuntimeError("OpenAI image response did not include base64 image data.")

        return base64.b64decode(image_b64)

    def _generate_from_text(self, prompt: str) -> bytes:
        response = self.openai_client.images.generate(
            model="gpt-image-1.5",
            prompt=prompt,
            size="1024x1024",
        )
        return self._extract_generated_image_bytes(response)

    def _generate_from_image(self, input_image: bytes, filename: str, prompt: str) -> bytes:
        image_buffer = io.BytesIO(input_image)
        image_buffer.name = filename

        response = self.openai_client.images.edit(
            model="gpt-image-1.5",
            image=image_buffer,
            prompt=prompt,
            size="1024x1024",
        )
        return self._extract_generated_image_bytes(response)

    async def _check_ai_image_limit(self, guild_id: int | None) -> tuple[bool, int, int]:
        if guild_id is None:
            return True, 0, 0

        mongo_cog = self.bot.get_cog("MongoDbCog")
        if mongo_cog is None:
            return True, 0, 0

        stats = await mongo_cog.get_current_month_stats(guild_id)
        if not stats:
            return True, 0, 50

        current_count = stats.get("ai_image_gen_count", 0)
        allowance = stats.get("aiimagegenallowance", 50)
        return current_count < allowance, current_count, allowance

    async def _build_buy_credits_url(self, guild_id: int | None) -> str:
        website_base_url = await self._get_website_base_url()
        return f"{website_base_url}/credits"

    async def generate_ai_image(self, interaction: discord.Interaction, message: discord.Message):
        if self.debug:
            guild_id_display = interaction.guild_id if interaction.guild_id is not None else "DM"
            print(
                f"[DEBUG] AI image request started by {interaction.user.name} ({interaction.user.id}) in guild {guild_id_display} for message {message.id}."
            )

        if interaction.guild_id and not await self._is_ai_image_enabled(interaction.guild_id):
            await interaction.response.send_message(
                "What a drag! 😢 Some square has disabled my awesome 'AI Image Generation' feature. Why not bug your admins to get it enabled again? 😏",
                ephemeral=True,
            )
            return

        if interaction.guild is not None and isinstance(interaction.channel, discord.TextChannel):
            ai_channel = await self._get_ai_image_channel(interaction.guild)
            if ai_channel is not None and interaction.channel.id != ai_channel.id:
                await interaction.response.send_message(
                    (
                        "Please use **Generate AI Image** only in "
                        f"{ai_channel.mention} so AI image requests stay in one place."
                    ),
                    ephemeral=True,
                )
                return

        if self.openai_client is None:
            if self.debug:
                print("[DEBUG] AI image request blocked: OPENAI_API_KEY is not configured.")
            await interaction.response.send_message(
                "OpenAI API key is not configured. Set OPENAI_API_KEY in environment variables.",
                ephemeral=True,
            )
            return

        can_generate, current_count, allowance = await self._check_ai_image_limit(interaction.guild_id)
        if not can_generate:
            if self.debug:
                print(
                    f"[DEBUG] AI image request blocked by allowance: {current_count}/{allowance} used this month."
                )
            await interaction.response.send_message(
                (
                    "⚠️ AI image generation limit exceeded! "
                    f"This server has used {current_count}/{allowance} images this month."
                ),
                ephemeral=True,
            )
            return

        credit_was_consumed = False
        remaining_user_credits = 0
        if interaction.guild_id is not None:
            mongo_cog = self.bot.get_cog("MongoDbCog")
            if mongo_cog is not None:
                consumed, balance_after = await mongo_cog.consume_user_ai_image_credit(
                    interaction.user.id,
                    str(interaction.user),
                )
                if not consumed:
                    buy_url = await self._build_buy_credits_url(interaction.guild_id)
                    await interaction.response.send_message(
                        (
                            "⚠️ You have no AI image credits left.\n"
                            f"Current balance: {balance_after}\n"
                            f"Buy more credits: {buy_url}"
                        ),
                        ephemeral=True,
                    )
                    return

                credit_was_consumed = True
                remaining_user_credits = balance_after

        prompt = (message.content or "").strip()
        image_attachment = self._get_image_attachment(message)

        if image_attachment is None and not prompt:
            if self.debug:
                print("[DEBUG] AI image request blocked: selected message has no text and no image attachment.")
            await interaction.response.send_message(
                "The selected message has no text prompt or image attachment to generate from.",
                ephemeral=True,
            )
            return

        if image_attachment is not None and not prompt:
            if self.debug:
                print("[DEBUG] AI image request blocked: image attachment provided without text instructions.")
            await interaction.response.send_message(
                "Please include text in the selected message describing how to transform the attached image.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        try:
            if image_attachment is not None:
                if self.debug:
                    print(
                        f"[DEBUG] AI image request mode: image edit using attachment {image_attachment.filename} with prompt length {len(prompt)}."
                    )
                input_image_bytes = await image_attachment.read()
                generated_image_bytes = await asyncio.to_thread(
                    self._generate_from_image,
                    input_image_bytes,
                    image_attachment.filename or "input.png",
                    prompt,
                )
            else:
                if self.debug:
                    print(f"[DEBUG] AI image request mode: text-only generation with prompt length {len(prompt)}.")
                generated_image_bytes = await asyncio.to_thread(self._generate_from_text, prompt)

            if interaction.guild_id is not None:
                mongo_cog = self.bot.get_cog("MongoDbCog")
                if mongo_cog is not None:
                    try:
                        new_count = await mongo_cog.increment_ai_image_count(interaction.guild_id)
                        if self.debug:
                            stats = await mongo_cog.get_current_month_stats(interaction.guild_id)
                            allowance_value = stats.get("aiimagegenallowance", 50) if stats else 50
                            remaining = max(allowance_value - new_count, 0)
                            print(
                                f"[DEBUG] AI image usage: used 1 image; total {new_count}/{allowance_value}; remaining {remaining}."
                            )
                    except Exception as error:
                        if self.debug:
                            print(f"[DEBUG] Error incrementing AI image count: {error}")

            discord_file = discord.File(io.BytesIO(generated_image_bytes), filename="generated-ai-image.png")
            await interaction.followup.send(
                content="Here's your AI-generated image, we hope you like it!",
                file=discord_file,
            )

            monthly_credits = 0
            active_subscriptions = 0
            mongo_cog = self.bot.get_cog("MongoDbCog")
            if mongo_cog is not None:
                try:
                    subscription_summary = await mongo_cog.get_user_ai_image_subscription_summary(interaction.user.id)
                    monthly_credits = int(subscription_summary.get("monthly_credits", 0) or 0)
                    active_subscriptions = int(subscription_summary.get("active_subscriptions", 0) or 0)
                except Exception as subscription_error:
                    if self.debug:
                        print(f"[DEBUG] Failed to fetch subscription summary: {subscription_error}")

            single_purchase_credits_remaining = max(remaining_user_credits - monthly_credits, 0)
            total_credits_this_month = single_purchase_credits_remaining + monthly_credits

            if self.debug:
                print(
                    "[DEBUG] AI image user credits: "
                    f"raw_wallet_balance={remaining_user_credits}, "
                    f"single_purchase_remaining={single_purchase_credits_remaining}, "
                    f"monthly_subscription_credits={monthly_credits}, "
                    f"total_credits_this_month={total_credits_this_month}, "
                    f"active_subscriptions={active_subscriptions}."
                )

            subscription_text = (
                f"Monthly subscription credits: {monthly_credits}/month "
                f"({active_subscriptions} active subscription{'s' if active_subscriptions != 1 else ''})"
                if active_subscriptions > 0
                else "No active subscription"
            )
            await interaction.followup.send(
                content=(
                    "Your AI credit details:\n"
                    f"- Single purchase credits remaining: {single_purchase_credits_remaining}\n"
                    f"- {subscription_text}\n"
                    f"- Total credits this month (single + subscription): {total_credits_this_month}"
                ),
                ephemeral=True,
            )
        except Exception as error:
            if credit_was_consumed and interaction.guild_id is not None:
                mongo_cog = self.bot.get_cog("MongoDbCog")
                if mongo_cog is not None:
                    try:
                        await mongo_cog.refund_user_ai_image_credit(interaction.user.id)
                    except Exception as refund_error:
                        if self.debug:
                            print(f"[DEBUG] Failed to refund AI image credit after generation error: {refund_error}")

            if self.debug:
                print(f"[DEBUG] AI image generation failed: {error}")
            await interaction.followup.send(f"Oops, there was an error generating the image: {error}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AiImageGenerationCog(bot))
