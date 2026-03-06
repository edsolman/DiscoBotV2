from __future__ import annotations

import os

import discord
from discord import app_commands
from discord.ext import commands


class WebsiteLinkCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.website_base_url = os.environ.get("WEBSITE_BASE_URL", "http://localhost:3000").rstrip("/")

    async def _get_website_base_url(self) -> str:
        mongo_cog = self.bot.get_cog("MongoDbCog")
        if mongo_cog and hasattr(mongo_cog, "get_owner_website_base_url"):
            try:
                return await mongo_cog.get_owner_website_base_url(self.website_base_url)
            except Exception:
                return self.website_base_url

        return self.website_base_url

    async def _send_website_link(self, interaction: discord.Interaction):
        website_url = await self._get_website_base_url()

        view = discord.ui.View()
        view.add_item(
            discord.ui.Button(
                label="Open DiscoBot Website",
                url=website_url,
            )
        )

        await interaction.response.send_message(
            f"Use the button below to open the DiscoBot website: {website_url}",
            view=view,
            ephemeral=True,
        )

    @app_commands.command(
        name="website",
        description="Open the DiscoBot website",
    )
    async def website(self, interaction: discord.Interaction):
        await self._send_website_link(interaction)


async def setup(bot: commands.Bot):
    await bot.add_cog(WebsiteLinkCog(bot))
