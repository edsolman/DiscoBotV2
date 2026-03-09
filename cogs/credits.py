from __future__ import annotations

from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands


class CreditsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @staticmethod
    def _to_int(value, default: int = 0) -> int:
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _fmt(value: int) -> str:
        return f"{max(int(value), 0):,}"

    @staticmethod
    def _active_statuses(mongo_cog) -> list[str]:
        statuses = getattr(mongo_cog, "ACTIVE_SUBSCRIPTION_STATUSES", ("active", "trialing", "past_due", "unpaid"))
        return [str(row) for row in statuses]

    @staticmethod
    def _user_candidates(user_id: int) -> list[object]:
        return [user_id, str(user_id)]

    @staticmethod
    def _guild_candidates(guild_id: int) -> list[object]:
        return [guild_id, str(guild_id)]

    async def _render_personal_summary(self, interaction: discord.Interaction, mongo_cog) -> discord.Embed:
        db = mongo_cog.client["discordguilds"]
        user_id = int(interaction.user.id)
        candidates = self._user_candidates(user_id)
        active_statuses = self._active_statuses(mongo_cog)

        ai_wallet = db["ai_image_user_credits"].find_one(
            {"user_id": {"$in": candidates}},
            {
                "ai_image_credits_balance": 1,
                "ai_image_credits_purchased_total": 1,
                "ai_image_credits_used_total": 1,
            },
        )
        ai_subscriptions = list(
            db["ai_image_credit_subscriptions"].find(
                {
                    "user_id": {"$in": candidates},
                    "status": {"$in": active_statuses},
                    "$or": [
                        {"purchase_scope": {"$exists": False}},
                        {"purchase_scope": "ai_user_personal"},
                        {"purchase_scope": ""},
                        {"purchase_scope": None},
                    ],
                },
                {"credits_per_month": 1},
            )
        )

        ai_balance = self._to_int((ai_wallet or {}).get("ai_image_credits_balance", 0))
        ai_purchased_total = self._to_int((ai_wallet or {}).get("ai_image_credits_purchased_total", 0))
        ai_used_total = self._to_int((ai_wallet or {}).get("ai_image_credits_used_total", 0))
        ai_sub_count = len(ai_subscriptions)
        ai_sub_monthly_total = sum(self._to_int(row.get("credits_per_month", 0)) for row in ai_subscriptions)

        translation_budget = await mongo_cog.check_personal_translation_character_budget(user_id, 0)
        translation_used = self._to_int(translation_budget.get("personal_used", 0))
        translation_allowance = self._to_int(translation_budget.get("personal_allowance", 0))
        translation_remaining = self._to_int(translation_budget.get("personal_remaining", 0))

        translation_subscriptions = list(
            db["translation_character_subscriptions"].find(
                {
                    "purchase_scope": "translation_user_personal",
                    "user_id": {"$in": candidates},
                    "status": {"$in": active_statuses},
                },
                {"characters_per_month": 1},
            )
        )
        translation_sub_count = len(translation_subscriptions)
        translation_sub_monthly_total = sum(self._to_int(row.get("characters_per_month", 0)) for row in translation_subscriptions)

        embed = discord.Embed(
            title="Personal Credits Summary",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="AI Credits",
            value=(
                f"Balance: **{self._fmt(ai_balance)}**\n"
                f"Purchased total: **{self._fmt(ai_purchased_total)}**\n"
                f"Used total: **{self._fmt(ai_used_total)}**\n"
                f"Active subscriptions: **{self._fmt(ai_sub_count)}**\n"
                f"Subscription credits/month: **{self._fmt(ai_sub_monthly_total)}**"
            ),
            inline=False,
        )
        embed.add_field(
            name="Translation Credits (Characters)",
            value=(
                f"Allowance this month: **{self._fmt(translation_allowance)}**\n"
                f"Used this month: **{self._fmt(translation_used)}**\n"
                f"Remaining this month: **{self._fmt(translation_remaining)}**\n"
                f"Active subscriptions: **{self._fmt(translation_sub_count)}**\n"
                f"Subscription characters/month: **{self._fmt(translation_sub_monthly_total)}**"
            ),
            inline=False,
        )
        embed.set_footer(text="All values are monthly where applicable.")
        return embed

    async def _render_guild_summary(self, interaction: discord.Interaction, mongo_cog) -> discord.Embed:
        assert interaction.guild is not None

        db = mongo_cog.client["discordguilds"]
        guild_id = int(interaction.guild.id)
        candidates = self._guild_candidates(guild_id)
        active_statuses = self._active_statuses(mongo_cog)

        guild_doc = db["guilds"].find_one(
            {"guild_id": {"$in": candidates}},
            {
                "guild_name": 1,
                "translationallowance": 1,
                "translationcharacterallowance": 1,
                "aiimagegenallowance": 1,
            },
        ) or {}

        now = datetime.utcnow()
        usage_doc = db["guild_data"].find_one(
            {
                "guild_id": {"$in": candidates},
                "year": now.year,
                "month": now.month,
            },
            {
                "translation_count": 1,
                "translation_character_count": 1,
                "ai_image_gen_count": 1,
            },
        ) or {}

        ai_allowance = self._to_int(guild_doc.get("aiimagegenallowance", 0))
        ai_used = self._to_int(usage_doc.get("ai_image_gen_count", 0))
        ai_remaining = max(ai_allowance - ai_used, 0)

        tr_msg_used = self._to_int(usage_doc.get("translation_count", 0))
        tr_char_allowance = self._to_int(guild_doc.get("translationcharacterallowance", 0))
        tr_char_used = self._to_int(usage_doc.get("translation_character_count", 0))
        tr_char_remaining = max(tr_char_allowance - tr_char_used, 0)

        ai_subscriptions = list(
            db["ai_image_credit_subscriptions"].find(
                {
                    "purchase_scope": "ai_guild",
                    "guild_id": {"$in": candidates},
                    "status": {"$in": active_statuses},
                },
                {"credits_per_month": 1},
            )
        )
        ai_sub_count = len(ai_subscriptions)
        ai_sub_monthly_total = sum(self._to_int(row.get("credits_per_month", 0)) for row in ai_subscriptions)

        tr_subscriptions = list(
            db["translation_character_subscriptions"].find(
                {
                    "$or": [
                        {"purchase_scope": {"$exists": False}},
                        {"purchase_scope": "translation_guild"},
                        {"purchase_scope": ""},
                        {"purchase_scope": None},
                    ],
                    "guild_id": {"$in": candidates},
                    "status": {"$in": active_statuses},
                },
                {"characters_per_month": 1},
            )
        )
        tr_sub_count = len(tr_subscriptions)
        tr_sub_monthly_total = sum(self._to_int(row.get("characters_per_month", 0)) for row in tr_subscriptions)

        embed = discord.Embed(
            title=f"Guild Credits Summary - {guild_doc.get('guild_name') or interaction.guild.name}",
            color=discord.Color.gold(),
        )
        embed.add_field(
            name="AI Credits",
            value=(
                f"Allowance this month: **{self._fmt(ai_allowance)}**\n"
                f"Used this month: **{self._fmt(ai_used)}**\n"
                f"Remaining this month: **{self._fmt(ai_remaining)}**\n"
                f"Active subscriptions: **{self._fmt(ai_sub_count)}**\n"
                f"Subscription credits/month: **{self._fmt(ai_sub_monthly_total)}**"
            ),
            inline=False,
        )
        embed.add_field(
            name="Translation Credits",
            value=(
                f"Messages translated: **{self._fmt(tr_msg_used)}**\n"
                f"Character allowance: **{self._fmt(tr_char_allowance)}**\n"
                f"Characters used: **{self._fmt(tr_char_used)}**\n"
                f"Characters remaining: **{self._fmt(tr_char_remaining)}**\n"
                f"Active subscriptions: **{self._fmt(tr_sub_count)}**\n"
                f"Subscription characters/month: **{self._fmt(tr_sub_monthly_total)}**"
            ),
            inline=False,
        )
        embed.set_footer(text="All values are monthly where applicable.")
        return embed

    @app_commands.command(name="credits", description="Show personal or guild AI/translation credits summary")
    @app_commands.describe(scope="Choose which credit scope to view")
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="Personal", value="personal"),
            app_commands.Choice(name="Guild", value="guild"),
        ]
    )
    async def credits(self, interaction: discord.Interaction, scope: app_commands.Choice[str]):
        mongo_cog = self.bot.get_cog("MongoDbCog")
        if mongo_cog is None:
            await interaction.response.send_message("Credit data is unavailable right now.", ephemeral=True)
            return

        selected_scope = str(scope.value or "personal").strip().lower()
        if selected_scope == "guild":
            if interaction.guild is None:
                await interaction.response.send_message(
                    "Sorry, you need to be an admin to view Guild credit information",
                    ephemeral=True,
                )
                return

            member = interaction.user if isinstance(interaction.user, discord.Member) else None
            is_admin = bool(member and member.guild_permissions.administrator)
            is_owner = interaction.guild.owner_id == interaction.user.id
            if not (is_admin or is_owner):
                await interaction.response.send_message(
                    "Sorry, you need to be an admin to view Guild credit information",
                    ephemeral=True,
                )
                return

            embed = await self._render_guild_summary(interaction, mongo_cog)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed = await self._render_personal_summary(interaction, mongo_cog)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(CreditsCog(bot))
