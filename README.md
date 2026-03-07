# DiscoBot

Discord bot for moderation, translation, gamification, scheduling, AI image generation, and website integration.

## Quick start

### Requirements

- Python 3.10+
- MongoDB connection string
- Discord bot token
- DeepL API key

### Install and run

1. Create and activate a virtual environment.
2. Install dependencies:
   - `pip install -r requirements.txt`
3. Set required environment variables.
4. Start the bot:
   - `python main.py`

### Required environment variables

- `DISCORD_TOKEN`
- `MONGODB_URI`
- `DEEPL_AUTH_KEY`

### Optional environment variables

- `OPENAI_API_KEY` (enables AI image generation; feature is unavailable without it)
- `WEBSITE_BASE_URL` (default: `http://localhost:3000`)
- `SINGLE_USER_MODE=true` (testing override for reputation cooldown/self-award checks)
- `DEBUG=true` (extra debug logging in supported cogs)

## Loaded cogs

- `translation`
- `mongodb`
- `ai_image_generation`
- `moderation`
- `gamification`
- `features`
- `scheduler`
- `website_link`

## Translation data files

The translation cog reads JSON data from:

- `data/translation/discord_locale_data.json`
- `data/translation/flag_emojis_unique.json`
- `data/translation/discord_flag_languages.json`
- `data/translation/deepl_target_languages.json`

These files are loaded through `cogs/translation_data.py` (single source of truth).

### How to update translation data

1. Edit the files in `data/translation/` only.
2. Keep file names unchanged unless you also update `cogs/translation_data.py`.
3. Preserve required keys:
   - `discord_locale_data.json`: `locale`, `deepLlocale`, `languageName`, `nativeName`, `flagEmoji`
   - `discord_flag_languages.json`: `deepLlocale`, `languageName`, `nativeName`, `flagEmoji`
   - `deepl_target_languages.json`: `language`
4. Restart the bot after changes.

### Notes

- Root-level duplicates were removed intentionally to avoid path ambiguity.
- If a required file/key is missing, startup will fail fast with a clear error.

## Moderation

The moderation cog scans messages for potentially rude/offensive wording and reports flagged messages to an admin-only channel named `admin-moderation` under the `DiscoBot-Admin` category.

Each moderation report includes:

- Author and channel details
- Running moderation stats for that user in the current guild:
   - Flagged count
   - Approved count
   - Rejected count
- A jump link to the original message
- Reasons the message was flagged
- Admin action buttons:
   - `Approve Message` (leave message in place)
   - `Remove Message` (delete the original message)
   - `Kick User` (reject + remove message + kick user)
   - `Ban User` (reject + remove message + ban user)

After an action is taken, the report is updated with the moderator + timestamp and the buttons are disabled.

### Excluding channels from moderation

Admins can exclude channels from moderation scans using slash commands:

- `/mod-exclude-add channel:#channel-name`
- `/mod-exclude-remove channel:#channel-name`
- `/mod-exclude-list`

Admins can also manage custom moderation words/phrases using slash commands:

- `/mod-word-add word:<text>`
- `/mod-word-remove word:<text>`
- `/mod-word-list`

Custom moderation words are stored per guild in MongoDB (`discordguilds.guilds.moderation_custom_terms`) and can also be managed in the website guild configuration page.

Exclusions are saved to `data/moderation/excluded_channels.json` and persist across bot restarts.

Moderation user stats are stored in MongoDB `discordguilds.user_data` with one document per `guild_id` + `user_id`.

### Required bot permissions

For moderation features to work correctly, the bot should have:

- `Manage Channels` (create/manage `DiscoBot` category and `admin-moderation` channel)
- `View Channels`
- `Send Messages`
- `Read Message History`
- `Manage Messages` (remove original messages when admins choose `Remove Message`)
- `Kick Members` (when admins choose `Kick User`)
- `Ban Members` (when admins choose `Ban User`)

## Gamification

The gamification cog awards XP as users chat, tracks levels, and posts a leaderboard in `leaderboard` under the public `DiscoBot` category.

### XP Commands

- `/xp [member]` — show XP profile (level, XP, reputation, counted messages)
- `/xp_leaderboard` — show XP + reputation leaderboard
- `/levels_show` — show current level configuration for the guild
- `/level_set level:<0-100> level_name:<text> interactions_required:<number>` — admin only, create/update level definitions
- `/levels_reset` — admin only, restore default level configuration

### Reputation Commands

- `/rep [member]` — show reputation points
- `/rep_give member:@user` — give +1 reputation (once per day, cannot self-award)
- `/rep_add member:@user points:<1-100>` — admin only, add reputation
- `/rep_remove member:@user points:<1-100>` — admin only, remove reputation

All XP, level, reputation, and moderation stats are stored per guild/per user in MongoDB collection `discordguilds.user_data`.

## Translation

Translation is available via message context menu commands:

- `Translate`
- `Translate (Private)`

The bot also supports flag-reaction translation using data from `data/translation/`.

## AI Image Credits

The AI image generation context-menu feature now enforces per-user credits per guild.

- Credits are read from `discordguilds.user_data` fields:
   - `ai_image_credits_balance`
   - `ai_image_credits_purchased_total`
   - `ai_image_credits_used_total`
- Each successful image generation consumes 1 credit.
- If generation fails after consumption, a refund is applied automatically.
- When a user has no credits left, the bot returns a link to the website credits page.

### Optional environment variable

- `WEBSITE_BASE_URL` (default: `http://localhost:3000`)
   - Used by the bot to build website links for buying credits.

### Testing override

Set environment variable `SINGLE_USER_MODE=true` to bypass `/rep_give` self-award and cooldown restrictions for testing on a single-user server.

## Scheduler

The scheduler cog allows users to schedule messages in a chosen channel with timezone-aware timing and recurrence.

### Scheduler Commands

- `/schedule_create channel:#channel message:<text> local_datetime:<YYYY-MM-DD HH:MM> timezone_name:<IANA timezone> recurrence:<once|daily|weekly>`
- `/schedule_list [show_all:true|false]` (show_all requires admin)
- `/schedule_delete schedule_id:<id>` (admins can delete any; users can delete their own)
- `/schedule_edit schedule_id:<id> channel:#channel message:<text> local_datetime:<YYYY-MM-DD HH:MM> timezone_name:<IANA timezone> recurrence:<once|daily|weekly>`
- `/schedule_pause schedule_id:<id>`
- `/schedule_resume schedule_id:<id>`

### Timezone examples

- `UTC`
- `Europe/London`
- `America/New_York`
- `Asia/Tokyo`

## Website Shortcut

- `/website` — posts a button that opens the DiscoBot website.

The URL target is configured by the bot owner in website Owner Settings (`Website Base URL`).
