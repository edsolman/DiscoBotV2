import os
import discord
from discord.ext import commands
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()
# Get the Discord token from environment variables
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]

# Set up intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True

# Create a bot instance
class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="/", intents=intents)

    async def setup_hook(self):
        await self.load_extension("cogs.translation")
        await self.load_extension("cogs.mongodb")
        await self.load_extension("cogs.ai_image_generation")
        await self.load_extension("cogs.moderation")
        await self.load_extension("cogs.gamification")
        await self.load_extension("cogs.features")
        await self.load_extension("cogs.scheduler")
        await self.load_extension("cogs.website_link")
        await self.tree.sync()

bot = MyBot()

# Event handler for when the bot is ready
@bot.event
async def on_ready():
    print(f"[INFO] Logged in as {bot.user.name} (ID: {bot.user.id})")
    
    # Ensure all existing guilds have database records
    mongo_cog = bot.get_cog("MongoDbCog")
    if mongo_cog:
        for guild in bot.guilds:
            try:
                was_inserted = await mongo_cog.ensure_guild_exists(guild.id, guild.name)
                if was_inserted:
                    print(f"[INFO] Created database record for existing guild: {guild.name}")
            except Exception as error:
                print(f"[ERROR] Error registering guild {guild.name}: {error}")
    else:
        print("[WARN] MongoDbCog not loaded")


# Event handler for when the bot joins a guild
@bot.event
async def on_guild_join(guild: discord.Guild):
    print(f"[INFO] Joined guild: {guild.name} (ID: {guild.id})")
    
    try:
        # Get the MongoDB cog
        mongo_cog = bot.get_cog("MongoDbCog")
        if mongo_cog:
            # Check and insert guild record if it doesn't exist
            was_inserted = await mongo_cog.ensure_guild_exists(guild.id, guild.name)
            if was_inserted:
                print(f"[INFO] Created new database record for guild: {guild.name}")
            else:
                print(f"[INFO] Guild {guild.name} already has a database record")
        else:
            print("[WARN] MongoDbCog not loaded, skipping guild registration")
    except Exception as error:
        print(f"[ERROR] Error registering guild {guild.name}: {error}")


bot.run(DISCORD_TOKEN)