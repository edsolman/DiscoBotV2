from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks


class SchedulerCog(commands.Cog):
    DATETIME_FORMAT = "%Y-%m-%d %H:%M"
    DATE_FORMAT = "%Y-%m-%d"
    TIME_FORMAT = "%H:%M"
    WEEKDAY_TOKENS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _get_mongo_cog(self):
        return self.bot.get_cog("MongoDbCog")

    @staticmethod
    def _parse_local_datetime(local_datetime_text: str, timezone_name: str) -> datetime:
        tz = ZoneInfo(timezone_name)
        naive_local = datetime.strptime(local_datetime_text, SchedulerCog.DATETIME_FORMAT)
        return naive_local.replace(tzinfo=tz)

    @staticmethod
    def _to_utc_naive(dt_aware: datetime) -> datetime:
        return dt_aware.astimezone(timezone.utc).replace(tzinfo=None)

    @staticmethod
    def _format_utc_for_display(dt_utc_naive: datetime | None) -> str:
        if dt_utc_naive is None:
            return "n/a"
        aware = dt_utc_naive.replace(tzinfo=timezone.utc)
        return aware.strftime("%Y-%m-%d %H:%M UTC")

    @staticmethod
    def _compute_next_run_after_send(
        recurrence: str,
        timezone_name: str,
        local_time_text: str,
        start_date_text: str = "",
        send_time_text: str = "",
        weekly_days: list[str] | None = None,
        end_date_text: str = "",
    ) -> datetime | None:
        if recurrence == "once":
            return None

        tz = ZoneInfo(timezone_name)
        now_local = datetime.now(tz)

        parsed_start = None
        if start_date_text:
            try:
                parsed_start = datetime.strptime(start_date_text, SchedulerCog.DATE_FORMAT).date()
            except Exception:
                parsed_start = None

        parsed_end = None
        if end_date_text:
            try:
                parsed_end = datetime.strptime(end_date_text, SchedulerCog.DATE_FORMAT).date()
            except Exception:
                parsed_end = None

        parsed_time = None
        if send_time_text:
            try:
                parsed_time = datetime.strptime(send_time_text, SchedulerCog.TIME_FORMAT)
            except Exception:
                parsed_time = None

        if parsed_time is None:
            template_local = datetime.strptime(local_time_text, SchedulerCog.DATETIME_FORMAT)
            parsed_time = template_local

        if recurrence == "daily":
            candidate_local = now_local.replace(
                hour=parsed_time.hour,
                minute=parsed_time.minute,
                second=0,
                microsecond=0,
            )
            if parsed_start and candidate_local.date() < parsed_start:
                candidate_local = candidate_local.replace(
                    year=parsed_start.year,
                    month=parsed_start.month,
                    day=parsed_start.day,
                )
            if candidate_local <= now_local:
                candidate_local = candidate_local + timedelta(days=1)
            if parsed_end and candidate_local.date() > parsed_end:
                return None
            return SchedulerCog._to_utc_naive(candidate_local)

        if recurrence == "weekly":
            tokens = [str(token or "").strip().lower() for token in (weekly_days or []) if str(token or "").strip()]
            if not tokens:
                try:
                    template_local = datetime.strptime(local_time_text, SchedulerCog.DATETIME_FORMAT)
                    tokens = [SchedulerCog.WEEKDAY_TOKENS[template_local.weekday()]]
                except Exception:
                    tokens = []

            allowed_weekdays = {
                index for index, token in enumerate(SchedulerCog.WEEKDAY_TOKENS) if token in set(tokens)
            }
            if not allowed_weekdays:
                return None

            base_local = now_local.replace(
                hour=parsed_time.hour,
                minute=parsed_time.minute,
                second=0,
                microsecond=0,
            )

            if parsed_start and base_local.date() < parsed_start:
                base_local = base_local.replace(
                    year=parsed_start.year,
                    month=parsed_start.month,
                    day=parsed_start.day,
                )

            for day_offset in range(0, 21):
                candidate_local = base_local + timedelta(days=day_offset)
                if candidate_local.weekday() not in allowed_weekdays:
                    continue
                if candidate_local <= now_local:
                    continue
                if parsed_end and candidate_local.date() > parsed_end:
                    return None
                return SchedulerCog._to_utc_naive(candidate_local)

            return None

        return None

    @staticmethod
    def _is_schedule_past_end_date(row: dict, now_utc: datetime) -> bool:
        end_date_text = str(row.get("end_date_text", "") or "").strip()
        timezone_name = str(row.get("timezone_name", "UTC") or "UTC").strip() or "UTC"
        next_run_at = row.get("next_run_at")

        if not end_date_text or not isinstance(next_run_at, datetime):
            return False

        try:
            end_date = datetime.strptime(end_date_text, SchedulerCog.DATE_FORMAT).date()
            next_local_date = next_run_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(timezone_name)).date()
        except Exception:
            return False

        if next_local_date > end_date:
            return True

        now_local_date = now_utc.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(timezone_name)).date()
        return now_local_date > end_date

    @app_commands.command(name="schedule_create", description="Schedule a message to be posted in a channel")
    @app_commands.describe(
        channel="Channel where the message should be sent",
        message="Message text to schedule",
        local_datetime="First send time in 'YYYY-MM-DD HH:MM'",
        timezone_name="IANA timezone (e.g. Europe/London, UTC)",
        recurrence="once, daily, or weekly",
    )
    @app_commands.choices(
        recurrence=[
            app_commands.Choice(name="once", value="once"),
            app_commands.Choice(name="daily", value="daily"),
            app_commands.Choice(name="weekly", value="weekly"),
        ]
    )
    @app_commands.guild_only()
    async def schedule_create(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        message: app_commands.Range[str, 1, 1800],
        local_datetime: str,
        timezone_name: str,
        recurrence: app_commands.Choice[str],
    ):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        if not channel.permissions_for(interaction.user).send_messages:
            await interaction.response.send_message(
                "You can only schedule messages in channels where you can send messages.",
                ephemeral=True,
            )
            return

        if not channel.permissions_for(interaction.guild.me).send_messages:
            await interaction.response.send_message(
                "I do not have permission to send messages in that channel.",
                ephemeral=True,
            )
            return

        try:
            local_dt = self._parse_local_datetime(local_datetime, timezone_name)
        except Exception:
            await interaction.response.send_message(
                "Invalid datetime or timezone. Use `YYYY-MM-DD HH:MM` and a valid timezone like `Europe/London`.",
                ephemeral=True,
            )
            return

        next_run_at = self._to_utc_naive(local_dt)
        now_utc = datetime.utcnow()
        if next_run_at <= now_utc:
            await interaction.response.send_message(
                "The first run time must be in the future.",
                ephemeral=True,
            )
            return

        mongo_cog = await self._get_mongo_cog()
        if mongo_cog is None:
            await interaction.response.send_message("Scheduler is unavailable right now.", ephemeral=True)
            return

        schedule_id = await mongo_cog.create_scheduled_message(
            guild_id=interaction.guild.id,
            guild_name=interaction.guild.name,
            channel_id=channel.id,
            creator_user_id=interaction.user.id,
            creator_username=str(interaction.user),
            message_content=message,
            recurrence=recurrence.value,
            timezone_name=timezone_name,
            local_time_text=local_datetime,
            next_run_at=next_run_at,
            start_date_text=local_dt.strftime(self.DATE_FORMAT),
            send_time_text=local_dt.strftime(self.TIME_FORMAT),
            weekly_days=[self.WEEKDAY_TOKENS[local_dt.weekday()]] if recurrence.value == "weekly" else [],
            end_date_text="",
        )

        await interaction.response.send_message(
            (
                f"✅ Scheduled message created (`{schedule_id}`).\n"
                f"Channel: {channel.mention}\n"
                f"Recurrence: **{recurrence.value}**\n"
                f"First run: **{self._format_utc_for_display(next_run_at)}** ({timezone_name})"
            ),
            ephemeral=True,
        )

    @app_commands.command(name="schedule_list", description="List scheduled messages for this guild")
    @app_commands.describe(show_all="Show all schedules (admins only)")
    @app_commands.guild_only()
    async def schedule_list(self, interaction: discord.Interaction, show_all: bool = False):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        mongo_cog = await self._get_mongo_cog()
        if mongo_cog is None:
            await interaction.response.send_message("Scheduler is unavailable right now.", ephemeral=True)
            return

        include_all = show_all and interaction.user.guild_permissions.administrator
        creator_filter = None if include_all else interaction.user.id

        rows = await mongo_cog.list_scheduled_messages(
            guild_id=interaction.guild.id,
            creator_user_id=creator_filter,
            include_inactive=False,
            limit=50,
        )

        if not rows:
            await interaction.response.send_message("No active scheduled messages found.", ephemeral=True)
            return

        lines: list[str] = []
        for row in rows:
            schedule_id = row.get("schedule_id", "")
            channel_id = int(row.get("channel_id", 0))
            recurrence = row.get("recurrence", "once")
            timezone_name = row.get("timezone_name", "UTC")
            next_run_at = row.get("next_run_at")
            creator_user_id = int(row.get("creator_user_id", 0))
            lines.append(
                (
                    f"- `{schedule_id}` | <#{channel_id}> | {recurrence} | "
                    f"next: {self._format_utc_for_display(next_run_at)} | tz: {timezone_name} | "
                    f"creator: <@{creator_user_id}>"
                )
            )

        payload = "Active schedules:\n" + "\n".join(lines)
        if len(payload) > 1800:
            payload = payload[:1750] + "\n... (truncated)"

        await interaction.response.send_message(payload, ephemeral=True)

    @app_commands.command(name="schedule_delete", description="Delete a scheduled message")
    @app_commands.describe(schedule_id="The schedule id returned by schedule_create")
    @app_commands.guild_only()
    async def schedule_delete(self, interaction: discord.Interaction, schedule_id: str):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        mongo_cog = await self._get_mongo_cog()
        if mongo_cog is None:
            await interaction.response.send_message("Scheduler is unavailable right now.", ephemeral=True)
            return

        deleted = await mongo_cog.delete_scheduled_message(
            guild_id=interaction.guild.id,
            schedule_id=schedule_id.strip(),
            requester_user_id=interaction.user.id,
            requester_is_admin=interaction.user.guild_permissions.administrator,
        )

        if not deleted:
            await interaction.response.send_message(
                "Schedule not found, or you do not have permission to delete it.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(f"✅ Deleted schedule `{schedule_id}`.", ephemeral=True)

    @app_commands.command(name="schedule_pause", description="Pause a scheduled message")
    @app_commands.describe(schedule_id="The schedule id to pause")
    @app_commands.guild_only()
    async def schedule_pause(self, interaction: discord.Interaction, schedule_id: str):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        mongo_cog = await self._get_mongo_cog()
        if mongo_cog is None:
            await interaction.response.send_message("Scheduler is unavailable right now.", ephemeral=True)
            return

        paused = await mongo_cog.pause_scheduled_message(
            guild_id=interaction.guild.id,
            schedule_id=schedule_id.strip(),
            requester_user_id=interaction.user.id,
            requester_is_admin=interaction.user.guild_permissions.administrator,
        )

        if not paused:
            await interaction.response.send_message(
                "Schedule not found, already paused, or you do not have permission.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(f"⏸️ Paused schedule `{schedule_id}`.", ephemeral=True)

    @app_commands.command(name="schedule_resume", description="Resume a paused scheduled message")
    @app_commands.describe(schedule_id="The schedule id to resume")
    @app_commands.guild_only()
    async def schedule_resume(self, interaction: discord.Interaction, schedule_id: str):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        mongo_cog = await self._get_mongo_cog()
        if mongo_cog is None:
            await interaction.response.send_message("Scheduler is unavailable right now.", ephemeral=True)
            return

        resumed = await mongo_cog.resume_scheduled_message(
            guild_id=interaction.guild.id,
            schedule_id=schedule_id.strip(),
            requester_user_id=interaction.user.id,
            requester_is_admin=interaction.user.guild_permissions.administrator,
        )

        if not resumed:
            await interaction.response.send_message(
                "Schedule not found, already active, or you do not have permission.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(f"▶️ Resumed schedule `{schedule_id}`.", ephemeral=True)

    @app_commands.command(name="schedule_edit", description="Edit an existing scheduled message")
    @app_commands.describe(
        schedule_id="The schedule id to edit",
        channel="Updated channel",
        message="Updated message text",
        local_datetime="Updated next send time in 'YYYY-MM-DD HH:MM'",
        timezone_name="Updated timezone (IANA, e.g. Europe/London)",
        recurrence="Updated recurrence",
    )
    @app_commands.choices(
        recurrence=[
            app_commands.Choice(name="once", value="once"),
            app_commands.Choice(name="daily", value="daily"),
            app_commands.Choice(name="weekly", value="weekly"),
        ]
    )
    @app_commands.guild_only()
    async def schedule_edit(
        self,
        interaction: discord.Interaction,
        schedule_id: str,
        channel: discord.TextChannel,
        message: app_commands.Range[str, 1, 1800],
        local_datetime: str,
        timezone_name: str,
        recurrence: app_commands.Choice[str],
    ):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        if not channel.permissions_for(interaction.user).send_messages:
            await interaction.response.send_message(
                "You can only schedule messages in channels where you can send messages.",
                ephemeral=True,
            )
            return

        if not channel.permissions_for(interaction.guild.me).send_messages:
            await interaction.response.send_message(
                "I do not have permission to send messages in that channel.",
                ephemeral=True,
            )
            return

        try:
            local_dt = self._parse_local_datetime(local_datetime, timezone_name)
        except Exception:
            await interaction.response.send_message(
                "Invalid datetime or timezone. Use `YYYY-MM-DD HH:MM` and a valid timezone like `Europe/London`.",
                ephemeral=True,
            )
            return

        next_run_at = self._to_utc_naive(local_dt)
        if next_run_at <= datetime.utcnow():
            await interaction.response.send_message("The next run time must be in the future.", ephemeral=True)
            return

        mongo_cog = await self._get_mongo_cog()
        if mongo_cog is None:
            await interaction.response.send_message("Scheduler is unavailable right now.", ephemeral=True)
            return

        updated = await mongo_cog.edit_scheduled_message(
            guild_id=interaction.guild.id,
            schedule_id=schedule_id.strip(),
            requester_user_id=interaction.user.id,
            requester_is_admin=interaction.user.guild_permissions.administrator,
            channel_id=channel.id,
            message_content=message,
            recurrence=recurrence.value,
            timezone_name=timezone_name,
            local_time_text=local_datetime,
            next_run_at=next_run_at,
            start_date_text=local_dt.strftime(self.DATE_FORMAT),
            send_time_text=local_dt.strftime(self.TIME_FORMAT),
            weekly_days=[self.WEEKDAY_TOKENS[local_dt.weekday()]] if recurrence.value == "weekly" else [],
            end_date_text="",
        )

        if not updated:
            await interaction.response.send_message(
                "Schedule not found, no changes detected, or you do not have permission.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            (
                f"✅ Updated schedule `{schedule_id}`.\n"
                f"Channel: {channel.mention}\n"
                f"Recurrence: **{recurrence.value}**\n"
                f"Next run: **{self._format_utc_for_display(next_run_at)}** ({timezone_name})"
            ),
            ephemeral=True,
        )

    @tasks.loop(seconds=30)
    async def schedule_dispatch_loop(self):
        mongo_cog = await self._get_mongo_cog()
        if mongo_cog is None:
            return

        now_utc = datetime.utcnow()
        try:
            due_rows = await mongo_cog.get_due_scheduled_messages(now_utc, limit=25)
        except Exception as error:
            print(f"[WARN] Scheduler: Failed retrieving due schedules: {error}")
            return

        for row in due_rows:
            guild_id = int(row.get("guild_id", 0))
            channel_id = int(row.get("channel_id", 0))
            schedule_id = str(row.get("schedule_id", ""))
            recurrence = str(row.get("recurrence", "once"))
            timezone_name = str(row.get("timezone_name", "UTC"))
            local_time_text = str(row.get("local_time_text", ""))
            start_date_text = str(row.get("start_date_text", ""))
            send_time_text = str(row.get("send_time_text", ""))
            weekly_days = row.get("weekly_days") if isinstance(row.get("weekly_days"), list) else []
            end_date_text = str(row.get("end_date_text", ""))
            message_content = str(row.get("message_content", ""))

            if self._is_schedule_past_end_date(row, now_utc):
                await mongo_cog.mark_scheduled_message_failure(
                    guild_id=guild_id,
                    schedule_id=schedule_id,
                    error_text="Schedule reached end date and was disabled",
                )
                await mongo_cog.mark_scheduled_message_run(
                    guild_id=guild_id,
                    schedule_id=schedule_id,
                    next_run_at=None,
                )
                continue

            guild = self.bot.get_guild(guild_id)
            if guild is None:
                await mongo_cog.mark_scheduled_message_failure(
                    guild_id=guild_id,
                    schedule_id=schedule_id,
                    error_text="Guild not found by bot",
                )
                continue

            channel = guild.get_channel(channel_id)
            if channel is None or not isinstance(channel, discord.TextChannel):
                await mongo_cog.mark_scheduled_message_failure(
                    guild_id=guild_id,
                    schedule_id=schedule_id,
                    error_text="Channel not found or inaccessible",
                )
                continue

            try:
                await channel.send(message_content)
            except Exception as error:
                await mongo_cog.mark_scheduled_message_failure(
                    guild_id=guild_id,
                    schedule_id=schedule_id,
                    error_text=f"Failed sending message: {error}",
                )
                continue

            try:
                next_run_at = self._compute_next_run_after_send(
                    recurrence=recurrence,
                    timezone_name=timezone_name,
                    local_time_text=local_time_text,
                    start_date_text=start_date_text,
                    send_time_text=send_time_text,
                    weekly_days=weekly_days,
                    end_date_text=end_date_text,
                )
            except Exception as error:
                await mongo_cog.mark_scheduled_message_failure(
                    guild_id=guild_id,
                    schedule_id=schedule_id,
                    error_text=f"Failed computing next run: {error}",
                )
                next_run_at = None

            await mongo_cog.mark_scheduled_message_run(
                guild_id=guild_id,
                schedule_id=schedule_id,
                next_run_at=next_run_at,
            )

    @schedule_dispatch_loop.before_loop
    async def before_schedule_dispatch_loop(self):
        await self.bot.wait_until_ready()

    async def cog_load(self):
        if not self.schedule_dispatch_loop.is_running():
            self.schedule_dispatch_loop.start()

    async def cog_unload(self):
        if self.schedule_dispatch_loop.is_running():
            self.schedule_dispatch_loop.cancel()


async def setup(bot: commands.Bot):
    await bot.add_cog(SchedulerCog(bot))
