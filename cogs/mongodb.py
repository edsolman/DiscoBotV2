from __future__ import annotations

import asyncio
import os
from typing import Any
from urllib.parse import urlparse

import discord
from discord.ext import commands
from pymongo import MongoClient


class MongoDbCog(commands.Cog):
    FEATURE_KEYS: tuple[str, ...] = (
        "moderation",
        "gamification",
        "ai_image",
        "translation",
        "scheduled_messages",
    )
    ACTIVE_SUBSCRIPTION_STATUSES: tuple[str, ...] = (
        "active",
        "trialing",
        "past_due",
        "unpaid",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._mongodb_uri = os.environ.get("MONGODB_URI")
        self._client: MongoClient | None = None

    async def cog_load(self):
        await asyncio.to_thread(self._connect)
        await self.ensure_user_data_indexes()
        await self.ensure_scheduler_indexes()

    async def cog_unload(self):
        if self._client is not None:
            await asyncio.to_thread(self._client.close)
            self._client = None

    def _connect(self):
        if not self._mongodb_uri:
            raise RuntimeError("MONGODB_URI is not set. Add it to your environment variables.")

        client = MongoClient(self._mongodb_uri, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
        self._client = client

    @property
    def client(self) -> MongoClient:
        if self._client is None:
            raise RuntimeError("MongoDB client is not connected.")
        return self._client

    @staticmethod
    def _normalize_website_base_url(value: str | None, fallback: str) -> str:
        candidate = str(value or "").strip().rstrip("/")
        if not candidate:
            return fallback

        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return fallback

        return candidate

    async def get_owner_website_base_url(self, fallback_url: str) -> str:
        def _get_url() -> str:
            db = self.client["discordguilds"]
            settings = db["bot_owner_settings"].find_one(
                {"_id": "global_owner_settings"},
                {"website.base_url": 1},
            )
            configured = None
            if isinstance(settings, dict):
                website = settings.get("website")
                if isinstance(website, dict):
                    configured = website.get("base_url")

            return self._normalize_website_base_url(configured, fallback_url)

        return await asyncio.to_thread(_get_url)

    async def ping(self):
        return await asyncio.to_thread(self.client.admin.command, "ping")

    async def count_documents(self, database: str, collection: str, query: dict[str, Any] | None = None) -> int:
        return await asyncio.to_thread(
            self.client[database][collection].count_documents,
            query or {},
        )

    async def find_documents(
        self,
        database: str,
        collection: str,
        query: dict[str, Any] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        query = query or {}

        def _run_find():
            cursor = self.client[database][collection].find(query).limit(limit)
            return list(cursor)

        return await asyncio.to_thread(_run_find)

    async def ensure_guild_exists(self, guild_id: int, guild_name: str) -> bool:
        """
        Check if a guild record exists in the database and insert one if it doesn't.
        
        Args:
            guild_id: The Discord guild ID
            guild_name: The Discord guild name
            
        Returns:
            bool: True if a new record was inserted, False if it already existed
        """
        def _check_and_insert():
            db = self.client["discordguilds"]
            collection = db["guilds"]
            
            # Check if guild already exists
            existing = collection.find_one({"guild_id": guild_id})
            if existing:
                return False
            
            # Insert new guild record
            guild_record = {
                "guild_id": guild_id,
                "guild_name": guild_name,
                "sku": "Free",
                "translationallowance": 500,
                "translationcharacterallowance": 10000,
                "aiimagegenallowance": 50
            }
            collection.insert_one(guild_record)
            return True
        
        return await asyncio.to_thread(_check_and_insert)

    @classmethod
    def _default_guild_features(cls) -> dict[str, dict[str, bool]]:
        return {feature_key: {"enabled": True} for feature_key in cls.FEATURE_KEYS}

    @classmethod
    def _normalize_guild_features(cls, raw_features: Any) -> dict[str, dict[str, bool]]:
        defaults = cls._default_guild_features()
        source = raw_features if isinstance(raw_features, dict) else {}

        normalized: dict[str, dict[str, bool]] = {}
        for feature_key, default_value in defaults.items():
            raw_feature_value = source.get(feature_key)
            if isinstance(raw_feature_value, dict):
                enabled = raw_feature_value.get("enabled", True) is not False
            else:
                enabled = default_value["enabled"]

            normalized[feature_key] = {"enabled": bool(enabled)}

        return normalized

    async def get_guild_features(self, guild_id: int) -> dict[str, dict[str, bool]]:
        def _get_features() -> dict[str, dict[str, bool]]:
            db = self.client["discordguilds"]
            guilds_collection = db["guilds"]
            doc = guilds_collection.find_one(
                {"guild_id": guild_id},
                {"guild_features": 1},
            )

            raw_features = doc.get("guild_features") if isinstance(doc, dict) else None
            return self._normalize_guild_features(raw_features)

        return await asyncio.to_thread(_get_features)

    async def set_guild_feature_enabled(
        self,
        guild_id: int,
        guild_name: str,
        installer_user_id: int,
        feature_key: str,
        enabled: bool,
    ) -> dict[str, dict[str, bool]]:
        if feature_key not in self.FEATURE_KEYS:
            raise ValueError(f"Unsupported feature key: {feature_key}")

        def _set_feature() -> dict[str, dict[str, bool]]:
            from datetime import datetime

            db = self.client["discordguilds"]
            guilds_collection = db["guilds"]
            now = datetime.utcnow()

            existing = guilds_collection.find_one(
                {"guild_id": guild_id},
                {"guild_features": 1},
            )
            next_features = self._normalize_guild_features(existing.get("guild_features") if isinstance(existing, dict) else None)
            next_features[feature_key] = {"enabled": bool(enabled)}

            guilds_collection.update_one(
                {"guild_id": guild_id},
                {
                    "$set": {
                        "guild_id": guild_id,
                        "guild_name": guild_name,
                        "guild_features": next_features,
                        "updated_at": now,
                    },
                    "$setOnInsert": {
                        "created_at": now,
                        "sku": "Free",
                        "translationallowance": 500,
                        "translationcharacterallowance": 10000,
                        "aiimagegenallowance": 50,
                        "installer_user_id": installer_user_id,
                    },
                },
                upsert=True,
            )

            return next_features

        return await asyncio.to_thread(_set_feature)

    async def ensure_guild_data_exists(self, guild_id: int, year: int, month: int) -> bool:
        """
        Check if a guild_data record exists for the specified month and create one if it doesn't.
        
        Args:
            guild_id: The Discord guild ID
            year: The year
            month: The month (1-12)
            
        Returns:
            bool: True if a new record was inserted, False if it already existed
        """
        def _check_and_insert():
            db = self.client["discordguilds"]
            collection = db["guild_data"]
            
            # Check if record already exists for this month
            existing = collection.find_one({
                "guild_id": guild_id,
                "year": year,
                "month": month
            })
            if existing:
                return False
            
            # Insert new monthly record
            monthly_record = {
                "guild_id": guild_id,
                "year": year,
                "month": month,
                "translation_count": 0,
                "translation_character_count": 0,
                "ai_image_gen_count": 0
            }
            collection.insert_one(monthly_record)
            return True
        
        return await asyncio.to_thread(_check_and_insert)

    async def check_translation_limit(self, guild_id: int) -> tuple[bool, int, int]:
        """
        Check if a guild has exceeded its translation allowance for the current month.
        
        Args:
            guild_id: The Discord guild ID
            
        Returns:
            tuple: (can_translate, current_count, allowance)
                - can_translate: True if translation is allowed, False if limit exceeded
                - current_count: Current number of translations this month
                - allowance: The guild's translation allowance
        """
        def _check_limit():
            from datetime import datetime
            
            db = self.client["discordguilds"]
            guilds_collection = db["guilds"]
            data_collection = db["guild_data"]
            
            # Get the guild's allowance
            guild = guilds_collection.find_one({"guild_id": guild_id})
            if not guild:
                # If guild doesn't exist, return default values
                return (True, 0, 500)
            
            allowance = guild.get("translationallowance", 500)
            
            # Get current month's usage
            now = datetime.utcnow()
            year = now.year
            month = now.month
            
            monthly_data = data_collection.find_one({
                "guild_id": guild_id,
                "year": year,
                "month": month
            })
            
            current_count = monthly_data.get("translation_count", 0) if monthly_data else 0
            can_translate = current_count < allowance
            
            return (can_translate, current_count, allowance)
        
        return await asyncio.to_thread(_check_limit)

    async def check_translation_character_limit(self, guild_id: int) -> tuple[bool, int, int]:
        """
        Check if a guild has exceeded its translation character allowance for the current month.

        Args:
            guild_id: The Discord guild ID

        Returns:
            tuple: (can_translate, current_count, allowance)
                - can_translate: True if translation is allowed, False if limit exceeded
                - current_count: Current number of translated characters this month
                - allowance: The guild's translation character allowance
        """
        def _check_limit():
            from datetime import datetime

            db = self.client["discordguilds"]
            guilds_collection = db["guilds"]
            data_collection = db["guild_data"]

            guild = guilds_collection.find_one({"guild_id": guild_id})
            if not guild:
                return (True, 0, 10000)

            allowance = guild.get("translationcharacterallowance", 10000)

            now = datetime.utcnow()
            year = now.year
            month = now.month

            monthly_data = data_collection.find_one({
                "guild_id": guild_id,
                "year": year,
                "month": month
            })

            current_count = monthly_data.get("translation_character_count", 0) if monthly_data else 0
            can_translate = current_count < allowance

            return (can_translate, current_count, allowance)

        return await asyncio.to_thread(_check_limit)

    async def check_translation_character_budget(
        self,
        guild_id: int,
        user_id: int,
        character_count: int,
    ) -> dict[str, Any]:
        def _check_budget() -> dict[str, Any]:
            from datetime import datetime

            db = self.client["discordguilds"]
            guilds_collection = db["guilds"]
            data_collection = db["guild_data"]
            subscriptions_collection = db["translation_character_subscriptions"]
            user_usage_collection = db["translation_character_user_usage"]

            safe_character_count = max(int(character_count or 0), 0)
            now = datetime.utcnow()
            year = now.year
            month = now.month

            guild = guilds_collection.find_one({"guild_id": guild_id})
            guild_allowance = int(guild.get("translationcharacterallowance", 10000)) if guild else 10000
            monthly_guild_data = data_collection.find_one({
                "guild_id": guild_id,
                "year": year,
                "month": month,
            })
            guild_used = int(monthly_guild_data.get("translation_character_count", 0)) if monthly_guild_data else 0
            guild_remaining = max(guild_allowance - guild_used, 0)

            user_id_candidates = [user_id, str(user_id)]
            personal_allowance_rows = list(
                subscriptions_collection.aggregate(
                    [
                        {
                            "$match": {
                                "purchase_scope": "translation_user_personal",
                                "user_id": {"$in": user_id_candidates},
                                "status": {"$in": list(self.ACTIVE_SUBSCRIPTION_STATUSES)},
                            }
                        },
                        {
                            "$group": {
                                "_id": None,
                                "characters_per_month": {"$sum": "$characters_per_month"},
                            }
                        },
                    ]
                )
            )
            personal_allowance = int(personal_allowance_rows[0].get("characters_per_month", 0)) if personal_allowance_rows else 0

            personal_usage_doc = user_usage_collection.find_one(
                {
                    "user_id": {"$in": user_id_candidates},
                    "year": year,
                    "month": month,
                },
                {"translation_character_count": 1},
            )
            personal_used = int(personal_usage_doc.get("translation_character_count", 0)) if personal_usage_doc else 0
            personal_remaining = max(personal_allowance - personal_used, 0)

            if guild_remaining >= safe_character_count:
                source = "guild"
                can_translate = True
            elif personal_remaining >= safe_character_count:
                source = "personal"
                can_translate = True
            else:
                source = "none"
                can_translate = False

            return {
                "can_translate": can_translate,
                "source": source,
                "character_count": safe_character_count,
                "guild_used": guild_used,
                "guild_allowance": guild_allowance,
                "guild_remaining": guild_remaining,
                "personal_used": personal_used,
                "personal_allowance": personal_allowance,
                "personal_remaining": personal_remaining,
            }

        return await asyncio.to_thread(_check_budget)

    async def consume_translation_character_usage(
        self,
        guild_id: int,
        user_id: int,
        username: str,
        character_count: int,
        source_hint: str | None = None,
    ) -> dict[str, Any]:
        def _consume_usage() -> dict[str, Any]:
            from datetime import datetime

            db = self.client["discordguilds"]
            guilds_collection = db["guilds"]
            data_collection = db["guild_data"]
            subscriptions_collection = db["translation_character_subscriptions"]
            user_usage_collection = db["translation_character_user_usage"]

            safe_character_count = max(int(character_count or 0), 0)
            if safe_character_count <= 0:
                return {
                    "consumed": False,
                    "source": "none",
                    "guild_used": 0,
                    "guild_allowance": 0,
                    "guild_remaining": 0,
                    "personal_used": 0,
                    "personal_allowance": 0,
                    "personal_remaining": 0,
                }

            now = datetime.utcnow()
            year = now.year
            month = now.month

            def get_budget_snapshot() -> dict[str, int]:
                guild = guilds_collection.find_one({"guild_id": guild_id})
                guild_allowance = int(guild.get("translationcharacterallowance", 10000)) if guild else 10000
                monthly_guild_data = data_collection.find_one({
                    "guild_id": guild_id,
                    "year": year,
                    "month": month,
                })
                guild_used = int(monthly_guild_data.get("translation_character_count", 0)) if monthly_guild_data else 0
                guild_remaining = max(guild_allowance - guild_used, 0)

                user_id_candidates = [user_id, str(user_id)]
                personal_allowance_rows = list(
                    subscriptions_collection.aggregate(
                        [
                            {
                                "$match": {
                                    "purchase_scope": "translation_user_personal",
                                    "user_id": {"$in": user_id_candidates},
                                    "status": {"$in": list(self.ACTIVE_SUBSCRIPTION_STATUSES)},
                                }
                            },
                            {
                                "$group": {
                                    "_id": None,
                                    "characters_per_month": {"$sum": "$characters_per_month"},
                                }
                            },
                        ]
                    )
                )
                personal_allowance = int(personal_allowance_rows[0].get("characters_per_month", 0)) if personal_allowance_rows else 0

                personal_usage_doc = user_usage_collection.find_one(
                    {
                        "user_id": {"$in": user_id_candidates},
                        "year": year,
                        "month": month,
                    },
                    {"translation_character_count": 1},
                )
                personal_used = int(personal_usage_doc.get("translation_character_count", 0)) if personal_usage_doc else 0
                personal_remaining = max(personal_allowance - personal_used, 0)

                return {
                    "guild_used": guild_used,
                    "guild_allowance": guild_allowance,
                    "guild_remaining": guild_remaining,
                    "personal_used": personal_used,
                    "personal_allowance": personal_allowance,
                    "personal_remaining": personal_remaining,
                }

            budget = get_budget_snapshot()
            ordered_sources = ["guild", "personal"]
            if source_hint == "personal" and budget["guild_remaining"] < safe_character_count:
                ordered_sources = ["personal", "guild"]

            consumed_source = "none"
            if "guild" in ordered_sources and budget["guild_remaining"] >= safe_character_count:
                data_collection.update_one(
                    {
                        "guild_id": guild_id,
                        "year": year,
                        "month": month,
                    },
                    {
                        "$setOnInsert": {
                            "guild_id": guild_id,
                            "year": year,
                            "month": month,
                            "ai_image_gen_count": 0,
                            "translation_count": 0,
                            "translation_character_count": 0,
                        },
                        "$inc": {
                            "translation_count": 1,
                            "translation_character_count": safe_character_count,
                        },
                    },
                    upsert=True,
                )
                consumed_source = "guild"
            elif "personal" in ordered_sources and budget["personal_remaining"] >= safe_character_count:
                data_collection.update_one(
                    {
                        "guild_id": guild_id,
                        "year": year,
                        "month": month,
                    },
                    {
                        "$setOnInsert": {
                            "guild_id": guild_id,
                            "year": year,
                            "month": month,
                            "ai_image_gen_count": 0,
                            "translation_count": 0,
                            "translation_character_count": 0,
                        },
                        "$inc": {
                            "translation_count": 1,
                        },
                    },
                    upsert=True,
                )

                user_usage_collection.update_one(
                    {
                        "user_id": user_id,
                        "year": year,
                        "month": month,
                    },
                    {
                        "$setOnInsert": {
                            "user_id": user_id,
                            "year": year,
                            "month": month,
                            "username": username,
                            "translation_character_count": 0,
                            "created_at": now,
                        },
                        "$set": {
                            "username": username,
                            "updated_at": now,
                        },
                        "$inc": {
                            "translation_character_count": safe_character_count,
                        },
                    },
                    upsert=True,
                )
                consumed_source = "personal"

            next_budget = get_budget_snapshot()
            return {
                "consumed": consumed_source != "none",
                "source": consumed_source,
                **next_budget,
            }

        return await asyncio.to_thread(_consume_usage)

    async def increment_translation_count(self, guild_id: int) -> int:
        """
        Increment the translation count for the current month.
        Ensures the monthly record exists before incrementing.
        
        Args:
            guild_id: The Discord guild ID
            
        Returns:
            int: The new translation count
        """
        def _increment():
            from datetime import datetime
            
            db = self.client["discordguilds"]
            collection = db["guild_data"]
            
            now = datetime.utcnow()
            year = now.year
            month = now.month
            
            # Ensure the monthly record exists
            record = collection.find_one({
                "guild_id": guild_id,
                "year": year,
                "month": month
            })
            
            if not record:
                # Create the record if it doesn't exist
                collection.insert_one({
                    "guild_id": guild_id,
                    "year": year,
                    "month": month,
                    "translation_count": 1,
                    "translation_character_count": 0,
                    "ai_image_gen_count": 0
                })
                return 1
            else:
                # Increment the count
                result = collection.update_one(
                    {
                        "guild_id": guild_id,
                        "year": year,
                        "month": month
                    },
                    {"$inc": {"translation_count": 1}}
                )
                # Return the new count
                updated_record = collection.find_one({
                    "guild_id": guild_id,
                    "year": year,
                    "month": month
                })
                return updated_record.get("translation_count", 0)
        
        return await asyncio.to_thread(_increment)

    async def increment_ai_image_count(self, guild_id: int) -> int:
        """
        Increment the AI image generation count for the current month.
        Ensures the monthly record exists before incrementing.
        
        Args:
            guild_id: The Discord guild ID
            
        Returns:
            int: The new AI image generation count
        """
        def _increment():
            from datetime import datetime
            
            db = self.client["discordguilds"]
            collection = db["guild_data"]
            
            now = datetime.utcnow()
            year = now.year
            month = now.month
            
            # Ensure the monthly record exists
            record = collection.find_one({
                "guild_id": guild_id,
                "year": year,
                "month": month
            })
            
            if not record:
                # Create the record if it doesn't exist
                collection.insert_one({
                    "guild_id": guild_id,
                    "year": year,
                    "month": month,
                    "translation_count": 0,
                    "translation_character_count": 0,
                    "ai_image_gen_count": 1
                })
                return 1
            else:
                # Increment the count
                result = collection.update_one(
                    {
                        "guild_id": guild_id,
                        "year": year,
                        "month": month
                    },
                    {"$inc": {"ai_image_gen_count": 1}}
                )
                # Return the new count
                updated_record = collection.find_one({
                    "guild_id": guild_id,
                    "year": year,
                    "month": month
                })
                return updated_record.get("ai_image_gen_count", 0)
        
        return await asyncio.to_thread(_increment)

    async def get_user_ai_image_credit_balance(self, user_id: int) -> int:
        def _get_balance() -> int:
            db = self.client["discordguilds"]
            collection = db["ai_image_user_credits"]

            doc = collection.find_one(
                {"user_id": user_id},
                {"ai_image_credits_balance": 1},
            )
            if not doc:
                return 0
            return max(int(doc.get("ai_image_credits_balance", 0)), 0)

        return await asyncio.to_thread(_get_balance)

    async def get_user_ai_image_subscription_summary(self, user_id: int) -> dict[str, int]:
        def _get_summary() -> dict[str, int]:
            db = self.client["discordguilds"]
            collection = db["ai_image_credit_subscriptions"]

            rows = list(
                collection.find(
                    {
                        "user_id": user_id,
                        "status": {"$in": ["active", "trialing", "past_due", "unpaid"]},
                    },
                    {
                        "credits_per_month": 1,
                    },
                )
            )

            monthly_credits = 0
            active_subscriptions = 0
            for row in rows:
                credits = int(row.get("credits_per_month", 0) or 0)
                if credits <= 0:
                    continue
                monthly_credits += credits
                active_subscriptions += 1

            return {
                "active_subscriptions": active_subscriptions,
                "monthly_credits": monthly_credits,
            }

        return await asyncio.to_thread(_get_summary)

    async def consume_user_ai_image_credit(
        self,
        user_id: int,
        username: str,
    ) -> tuple[bool, int]:
        def _consume() -> tuple[bool, int]:
            from datetime import datetime

            db = self.client["discordguilds"]
            collection = db["ai_image_user_credits"]
            now = datetime.utcnow()

            result = collection.update_one(
                {
                    "user_id": user_id,
                    "ai_image_credits_balance": {"$gte": 1},
                },
                {
                    "$set": {
                        "username": username,
                        "updated_at": now,
                    },
                    "$inc": {
                        "ai_image_credits_balance": -1,
                        "ai_image_credits_used_total": 1,
                    },
                },
            )

            if result.modified_count == 0:
                existing = collection.find_one(
                    {"user_id": user_id},
                    {"ai_image_credits_balance": 1},
                )
                balance = max(int(existing.get("ai_image_credits_balance", 0)), 0) if existing else 0
                return False, balance

            updated = collection.find_one(
                {"user_id": user_id},
                {"ai_image_credits_balance": 1},
            )
            balance = max(int(updated.get("ai_image_credits_balance", 0)), 0) if updated else 0
            return True, balance

        return await asyncio.to_thread(_consume)

    async def refund_user_ai_image_credit(
        self,
        user_id: int,
    ) -> int:
        def _refund() -> int:
            from datetime import datetime

            db = self.client["discordguilds"]
            collection = db["ai_image_user_credits"]
            now = datetime.utcnow()

            collection.update_one(
                {"user_id": user_id},
                {
                    "$set": {"updated_at": now},
                    "$inc": {
                        "ai_image_credits_balance": 1,
                        "ai_image_credits_used_total": -1,
                    },
                },
            )

            updated = collection.find_one(
                {"user_id": user_id},
                {"ai_image_credits_balance": 1},
            )
            return max(int(updated.get("ai_image_credits_balance", 0)), 0) if updated else 0

        return await asyncio.to_thread(_refund)

    async def get_current_month_stats(self, guild_id: int) -> dict[str, Any]:
        """
        Get the usage statistics for the current month for a guild.
        
        Args:
            guild_id: The Discord guild ID
            
        Returns:
            dict: Contains guild_name, translationallowance, translation_count, ai_image_gen_count, year, month
        """
        def _get_stats():
            from datetime import datetime
            
            db = self.client["discordguilds"]
            guilds_collection = db["guilds"]
            data_collection = db["guild_data"]
            
            # Get guild info
            guild = guilds_collection.find_one({"guild_id": guild_id})
            if not guild:
                return None
            
            # Get current month's stats
            now = datetime.utcnow()
            year = now.year
            month = now.month
            
            monthly_data = data_collection.find_one({
                "guild_id": guild_id,
                "year": year,
                "month": month
            })
            
            stats = {
                "guild_name": guild.get("guild_name", "Unknown"),
                "translationallowance": guild.get("translationallowance", 500),
                "translationcharacterallowance": guild.get("translationcharacterallowance", 10000),
                "aiimagegenallowance": guild.get("aiimagegenallowance", 50),
                "translation_count": monthly_data.get("translation_count", 0) if monthly_data else 0,
                "translation_character_count": monthly_data.get("translation_character_count", 0) if monthly_data else 0,
                "ai_image_gen_count": monthly_data.get("ai_image_gen_count", 0) if monthly_data else 0,
                "year": year,
                "month": month
            }
            
            return stats
        
        return await asyncio.to_thread(_get_stats)

    async def increment_translation_character_count(self, guild_id: int, character_count: int) -> int:
        """
        Increment the translated character count for the current month.
        Ensures the monthly record exists before incrementing.

        Args:
            guild_id: The Discord guild ID
            character_count: Number of characters to add

        Returns:
            int: The new translated character count
        """
        def _increment():
            from datetime import datetime

            db = self.client["discordguilds"]
            collection = db["guild_data"]

            now = datetime.utcnow()
            year = now.year
            month = now.month

            record = collection.find_one({
                "guild_id": guild_id,
                "year": year,
                "month": month
            })

            if not record:
                collection.insert_one({
                    "guild_id": guild_id,
                    "year": year,
                    "month": month,
                    "translation_count": 0,
                    "translation_character_count": character_count,
                    "ai_image_gen_count": 0
                })
                return character_count

            collection.update_one(
                {
                    "guild_id": guild_id,
                    "year": year,
                    "month": month
                },
                {"$inc": {"translation_character_count": character_count}}
            )

            updated_record = collection.find_one({
                "guild_id": guild_id,
                "year": year,
                "month": month
            })
            return updated_record.get("translation_character_count", 0)

        return await asyncio.to_thread(_increment)

    async def backfill_translation_character_fields(self) -> tuple[int, int]:
        """
        Add missing translation character fields to existing guild and monthly records.

        Returns:
            tuple: (guilds_updated, monthly_updated)
        """
        def _backfill():
            db = self.client["discordguilds"]
            guilds_collection = db["guilds"]
            data_collection = db["guild_data"]

            guilds_result = guilds_collection.update_many(
                {"translationcharacterallowance": {"$exists": False}},
                {"$set": {"translationcharacterallowance": 10000}},
            )
            monthly_result = data_collection.update_many(
                {"translation_character_count": {"$exists": False}},
                {"$set": {"translation_character_count": 0}},
            )

            return guilds_result.modified_count, monthly_result.modified_count

        return await asyncio.to_thread(_backfill)

    async def ensure_user_data_indexes(self):
        def _ensure_indexes():
            db = self.client["discordguilds"]
            collection = db["user_data"]
            ai_image_user_credits_collection = db["ai_image_user_credits"]
            translation_user_usage_collection = db["translation_character_user_usage"]
            translation_subscriptions_collection = db["translation_character_subscriptions"]
            collection.create_index(
                [("guild_id", 1), ("user_id", 1)],
                unique=True,
                name="guild_user_unique_idx",
            )
            collection.create_index(
                [("guild_id", 1), ("moderation_flag_count", -1)],
                name="guild_moderation_count_idx",
            )
            collection.create_index(
                [("guild_id", 1), ("xp", -1)],
                name="guild_xp_idx",
            )
            collection.create_index(
                [("guild_id", 1), ("reputation", -1)],
                name="guild_reputation_idx",
            )
            collection.create_index(
                [("guild_id", 1), ("level", -1)],
                name="guild_level_idx",
            )
            ai_image_user_credits_collection.create_index(
                [("user_id", 1)],
                unique=True,
                name="user_credit_wallet_unique_idx",
            )
            translation_user_usage_collection.create_index(
                [("user_id", 1), ("year", 1), ("month", 1)],
                unique=True,
                name="translation_user_month_unique_idx",
            )
            translation_subscriptions_collection.create_index(
                [("purchase_scope", 1), ("user_id", 1), ("status", 1)],
                name="translation_personal_subscription_status_idx",
            )

        return await asyncio.to_thread(_ensure_indexes)

    async def ensure_scheduler_indexes(self):
        def _ensure_indexes():
            db = self.client["discordguilds"]
            collection = db["scheduled_messages"]
            collection.create_index(
                [("guild_id", 1), ("schedule_id", 1)],
                unique=True,
                name="guild_schedule_unique_idx",
            )
            collection.create_index(
                [("active", 1), ("next_run_at", 1)],
                name="active_next_run_idx",
            )
            collection.create_index(
                [("guild_id", 1), ("creator_user_id", 1), ("next_run_at", 1)],
                name="guild_creator_next_run_idx",
            )

        return await asyncio.to_thread(_ensure_indexes)

    async def create_scheduled_message(
        self,
        guild_id: int,
        guild_name: str,
        channel_id: int,
        creator_user_id: int,
        creator_username: str,
        message_content: str,
        recurrence: str,
        timezone_name: str,
        local_time_text: str,
        next_run_at,
        start_date_text: str = "",
        send_time_text: str = "",
        weekly_days: list[str] | None = None,
        end_date_text: str = "",
    ) -> str:
        def _create():
            from datetime import datetime
            from uuid import uuid4

            db = self.client["discordguilds"]
            collection = db["scheduled_messages"]
            now = datetime.utcnow()
            schedule_id = str(uuid4())

            collection.insert_one(
                {
                    "schedule_id": schedule_id,
                    "guild_id": guild_id,
                    "guild_name": guild_name,
                    "channel_id": channel_id,
                    "creator_user_id": creator_user_id,
                    "creator_username": creator_username,
                    "message_content": message_content,
                    "recurrence": recurrence,
                    "timezone_name": timezone_name,
                    "local_time_text": local_time_text,
                    "start_date_text": start_date_text,
                    "send_time_text": send_time_text,
                    "weekly_days": list(weekly_days or []),
                    "end_date_text": end_date_text,
                    "next_run_at": next_run_at,
                    "active": True,
                    "created_at": now,
                    "updated_at": now,
                    "last_run_at": None,
                    "run_count": 0,
                }
            )

            return schedule_id

        return await asyncio.to_thread(_create)

    async def list_scheduled_messages(
        self,
        guild_id: int,
        creator_user_id: int | None = None,
        include_inactive: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        def _list():
            db = self.client["discordguilds"]
            collection = db["scheduled_messages"]

            query: dict[str, Any] = {"guild_id": guild_id}
            if creator_user_id is not None:
                query["creator_user_id"] = creator_user_id
            if not include_inactive:
                query["active"] = True

            cursor = collection.find(query).sort("next_run_at", 1).limit(max(int(limit), 1))

            rows: list[dict[str, Any]] = []
            for doc in cursor:
                rows.append(
                    {
                        "schedule_id": str(doc.get("schedule_id", "")),
                        "guild_id": int(doc.get("guild_id", 0)),
                        "channel_id": int(doc.get("channel_id", 0)),
                        "creator_user_id": int(doc.get("creator_user_id", 0)),
                        "creator_username": str(doc.get("creator_username", "Unknown")),
                        "message_content": str(doc.get("message_content", "")),
                        "recurrence": str(doc.get("recurrence", "once")),
                        "timezone_name": str(doc.get("timezone_name", "UTC")),
                        "local_time_text": str(doc.get("local_time_text", "")),
                        "start_date_text": str(doc.get("start_date_text", "")),
                        "send_time_text": str(doc.get("send_time_text", "")),
                        "weekly_days": list(doc.get("weekly_days") or []),
                        "end_date_text": str(doc.get("end_date_text", "")),
                        "next_run_at": doc.get("next_run_at"),
                        "active": bool(doc.get("active", True)),
                        "run_count": int(doc.get("run_count", 0)),
                    }
                )

            return rows

        return await asyncio.to_thread(_list)

    async def delete_scheduled_message(
        self,
        guild_id: int,
        schedule_id: str,
        requester_user_id: int,
        requester_is_admin: bool,
    ) -> bool:
        def _delete():
            db = self.client["discordguilds"]
            collection = db["scheduled_messages"]

            query: dict[str, Any] = {
                "guild_id": guild_id,
                "schedule_id": schedule_id,
            }
            if not requester_is_admin:
                query["creator_user_id"] = requester_user_id

            result = collection.delete_one(query)
            return result.deleted_count > 0

        return await asyncio.to_thread(_delete)

    async def pause_scheduled_message(
        self,
        guild_id: int,
        schedule_id: str,
        requester_user_id: int,
        requester_is_admin: bool,
    ) -> bool:
        def _pause():
            from datetime import datetime

            db = self.client["discordguilds"]
            collection = db["scheduled_messages"]

            query: dict[str, Any] = {
                "guild_id": guild_id,
                "schedule_id": schedule_id,
            }
            if not requester_is_admin:
                query["creator_user_id"] = requester_user_id

            result = collection.update_one(
                query,
                {
                    "$set": {
                        "active": False,
                        "updated_at": datetime.utcnow(),
                    }
                },
            )
            return result.modified_count > 0

        return await asyncio.to_thread(_pause)

    async def resume_scheduled_message(
        self,
        guild_id: int,
        schedule_id: str,
        requester_user_id: int,
        requester_is_admin: bool,
    ) -> bool:
        def _resume():
            from datetime import datetime

            db = self.client["discordguilds"]
            collection = db["scheduled_messages"]

            query: dict[str, Any] = {
                "guild_id": guild_id,
                "schedule_id": schedule_id,
                "next_run_at": {"$gt": datetime.utcnow()},
            }
            if not requester_is_admin:
                query["creator_user_id"] = requester_user_id

            result = collection.update_one(
                query,
                {
                    "$set": {
                        "active": True,
                        "updated_at": datetime.utcnow(),
                    }
                },
            )
            return result.modified_count > 0

        return await asyncio.to_thread(_resume)

    async def edit_scheduled_message(
        self,
        guild_id: int,
        schedule_id: str,
        requester_user_id: int,
        requester_is_admin: bool,
        channel_id: int,
        message_content: str,
        recurrence: str,
        timezone_name: str,
        local_time_text: str,
        next_run_at,
        start_date_text: str = "",
        send_time_text: str = "",
        weekly_days: list[str] | None = None,
        end_date_text: str = "",
    ) -> bool:
        def _edit():
            from datetime import datetime

            db = self.client["discordguilds"]
            collection = db["scheduled_messages"]

            query: dict[str, Any] = {
                "guild_id": guild_id,
                "schedule_id": schedule_id,
            }
            if not requester_is_admin:
                query["creator_user_id"] = requester_user_id

            result = collection.update_one(
                query,
                {
                    "$set": {
                        "channel_id": channel_id,
                        "message_content": message_content,
                        "recurrence": recurrence,
                        "timezone_name": timezone_name,
                        "local_time_text": local_time_text,
                        "start_date_text": start_date_text,
                        "send_time_text": send_time_text,
                        "weekly_days": list(weekly_days or []),
                        "end_date_text": end_date_text,
                        "next_run_at": next_run_at,
                        "active": True,
                        "updated_at": datetime.utcnow(),
                    }
                },
            )
            return result.modified_count > 0

        return await asyncio.to_thread(_edit)

    async def get_due_scheduled_messages(self, now_utc, limit: int = 20) -> list[dict[str, Any]]:
        def _get_due():
            db = self.client["discordguilds"]
            collection = db["scheduled_messages"]

            cursor = (
                collection.find(
                    {
                        "active": True,
                        "next_run_at": {"$lte": now_utc},
                    }
                )
                .sort("next_run_at", 1)
                .limit(max(int(limit), 1))
            )

            rows: list[dict[str, Any]] = []
            for doc in cursor:
                rows.append(
                    {
                        "schedule_id": str(doc.get("schedule_id", "")),
                        "guild_id": int(doc.get("guild_id", 0)),
                        "channel_id": int(doc.get("channel_id", 0)),
                        "creator_user_id": int(doc.get("creator_user_id", 0)),
                        "message_content": str(doc.get("message_content", "")),
                        "recurrence": str(doc.get("recurrence", "once")),
                        "timezone_name": str(doc.get("timezone_name", "UTC")),
                        "local_time_text": str(doc.get("local_time_text", "")),
                        "start_date_text": str(doc.get("start_date_text", "")),
                        "send_time_text": str(doc.get("send_time_text", "")),
                        "weekly_days": list(doc.get("weekly_days") or []),
                        "end_date_text": str(doc.get("end_date_text", "")),
                        "next_run_at": doc.get("next_run_at"),
                        "run_count": int(doc.get("run_count", 0)),
                    }
                )

            return rows

        return await asyncio.to_thread(_get_due)

    async def mark_scheduled_message_run(
        self,
        guild_id: int,
        schedule_id: str,
        next_run_at,
    ):
        def _mark_run():
            from datetime import datetime

            db = self.client["discordguilds"]
            collection = db["scheduled_messages"]
            now = datetime.utcnow()

            if next_run_at is None:
                collection.update_one(
                    {"guild_id": guild_id, "schedule_id": schedule_id},
                    {
                        "$set": {
                            "active": False,
                            "last_run_at": now,
                            "updated_at": now,
                        },
                        "$inc": {"run_count": 1},
                    },
                )
            else:
                collection.update_one(
                    {"guild_id": guild_id, "schedule_id": schedule_id},
                    {
                        "$set": {
                            "next_run_at": next_run_at,
                            "last_run_at": now,
                            "updated_at": now,
                        },
                        "$inc": {"run_count": 1},
                    },
                )

        await asyncio.to_thread(_mark_run)

    async def mark_scheduled_message_failure(self, guild_id: int, schedule_id: str, error_text: str):
        def _mark_failure():
            from datetime import datetime

            db = self.client["discordguilds"]
            collection = db["scheduled_messages"]
            now = datetime.utcnow()

            collection.update_one(
                {"guild_id": guild_id, "schedule_id": schedule_id},
                {
                    "$set": {
                        "last_error": error_text[:500],
                        "updated_at": now,
                    }
                },
            )

        await asyncio.to_thread(_mark_failure)

    @staticmethod
    def _default_gamification_levels() -> list[dict[str, Any]]:
        return [
            {"level": 0, "name": "Newcomer", "interactions_required": 0},
            {"level": 1, "name": "Explorer", "interactions_required": 10},
            {"level": 2, "name": "Regular", "interactions_required": 30},
            {"level": 3, "name": "Veteran", "interactions_required": 75},
            {"level": 4, "name": "Elite", "interactions_required": 150},
        ]

    @staticmethod
    def _default_bot_owner_settings() -> dict[str, Any]:
        return {
            "channels": {
                "gamification": {
                    "category_name": "DiscoBot",
                    "leaderboard_channel_name": "leaderboard",
                },
                "moderation": {
                    "category_name": "DiscoBot-Admin",
                    "channel_name": "admin-moderation",
                    "channel_description": (
                        "Welcome to the DiscoBot Admin Moderation channel. This channel is only visible to "
                        "administrators and will flag up any potentially offensive, rude or otherwise unwanted "
                        "messages posted by users. You will be able to approve or reject and remove them. A log "
                        "will be kept of how many moderated comments are made per user, and how many of these were "
                        "approved/rejected, to make it easy to see if you have any specific users regularly posting "
                        "offensive content. You will also have the opportunity to directly kick/ban a user from the "
                        "guild if required and it will send them a direct message informing them why they were "
                        "kicked/banned."
                    ),
                },
                "ai_image": {
                    "category_name": "DiscoBot",
                    "channel_name": "ai-image-generation",
                    "channel_topic": (
                        "Generate AI images by right-clicking a message and selecting 'Apps > Generate AI Image'. "
                        "Use text-only for new images, or include text + an attached image to generate from that image."
                    ),
                },
            }
        }

    async def get_bot_owner_settings(self) -> dict[str, Any]:
        def _get_settings() -> dict[str, Any]:
            defaults = self._default_bot_owner_settings()

            db = self.client["discordguilds"]
            collection = db["bot_owner_settings"]
            doc = collection.find_one({"_id": "global_owner_settings"}) or {}

            channels = doc.get("channels") if isinstance(doc.get("channels"), dict) else {}

            gamification_raw = channels.get("gamification") if isinstance(channels.get("gamification"), dict) else {}
            moderation_raw = channels.get("moderation") if isinstance(channels.get("moderation"), dict) else {}
            ai_image_raw = channels.get("ai_image") if isinstance(channels.get("ai_image"), dict) else {}

            gamification_defaults = defaults["channels"]["gamification"]
            moderation_defaults = defaults["channels"]["moderation"]
            ai_image_defaults = defaults["channels"]["ai_image"]

            return {
                "channels": {
                    "gamification": {
                        "category_name": str(gamification_raw.get("category_name") or gamification_defaults["category_name"]),
                        "leaderboard_channel_name": str(
                            gamification_raw.get("leaderboard_channel_name") or gamification_defaults["leaderboard_channel_name"]
                        ),
                    },
                    "moderation": {
                        "category_name": str(moderation_raw.get("category_name") or moderation_defaults["category_name"]),
                        "channel_name": str(moderation_raw.get("channel_name") or moderation_defaults["channel_name"]),
                        "channel_description": str(
                            moderation_raw.get("channel_description") or moderation_defaults["channel_description"]
                        ),
                    },
                    "ai_image": {
                        "category_name": str(ai_image_raw.get("category_name") or ai_image_defaults["category_name"]),
                        "channel_name": str(ai_image_raw.get("channel_name") or ai_image_defaults["channel_name"]),
                        "channel_topic": str(ai_image_raw.get("channel_topic") or ai_image_defaults["channel_topic"]),
                    },
                }
            }

        return await asyncio.to_thread(_get_settings)

    @staticmethod
    def _sanitize_custom_moderation_term(term_raw: Any) -> str | None:
        term = str(term_raw or "").strip().lower()
        if not term:
            return None

        term = " ".join(term.split())
        if len(term) < 2 or len(term) > 64:
            return None

        return term

    async def get_guild_custom_moderation_terms(self, guild_id: int) -> list[str]:
        def _get_terms() -> list[str]:
            db = self.client["discordguilds"]
            guilds_collection = db["guilds"]
            doc = guilds_collection.find_one(
                {"guild_id": guild_id},
                {"moderation_custom_terms": 1},
            )

            raw_terms = doc.get("moderation_custom_terms") if doc else None
            if not isinstance(raw_terms, list) or len(raw_terms) == 0:
                return []

            normalized_terms: list[str] = []
            seen: set[str] = set()
            for raw_term in raw_terms:
                term = self._sanitize_custom_moderation_term(raw_term)
                if term is None or term in seen:
                    continue
                seen.add(term)
                normalized_terms.append(term)

            normalized_terms.sort()
            return normalized_terms

        return await asyncio.to_thread(_get_terms)

    async def add_guild_custom_moderation_term(
        self,
        guild_id: int,
        guild_name: str,
        term_raw: str,
    ) -> tuple[bool, list[str], str | None]:
        def _add_term() -> tuple[bool, list[str], str | None]:
            from datetime import datetime

            term = self._sanitize_custom_moderation_term(term_raw)
            if term is None:
                return False, [], None

            db = self.client["discordguilds"]
            guilds_collection = db["guilds"]
            now = datetime.utcnow()

            result = guilds_collection.update_one(
                {"guild_id": guild_id},
                {
                    "$set": {
                        "guild_id": guild_id,
                        "guild_name": guild_name,
                        "updated_at": now,
                    },
                    "$setOnInsert": {
                        "created_at": now,
                        "sku": "Free",
                        "translationallowance": 500,
                        "translationcharacterallowance": 10000,
                        "aiimagegenallowance": 50,
                    },
                    "$addToSet": {
                        "moderation_custom_terms": term,
                    },
                },
                upsert=True,
            )

            doc = guilds_collection.find_one(
                {"guild_id": guild_id},
                {"moderation_custom_terms": 1},
            )
            raw_terms = doc.get("moderation_custom_terms") if doc else []
            normalized_terms: list[str] = []
            seen: set[str] = set()
            for raw_item in raw_terms if isinstance(raw_terms, list) else []:
                normalized = self._sanitize_custom_moderation_term(raw_item)
                if normalized is None or normalized in seen:
                    continue
                seen.add(normalized)
                normalized_terms.append(normalized)

            normalized_terms.sort()
            was_added = result.modified_count > 0 or result.upserted_id is not None
            return was_added, normalized_terms, term

        return await asyncio.to_thread(_add_term)

    async def remove_guild_custom_moderation_term(
        self,
        guild_id: int,
        term_raw: str,
    ) -> tuple[bool, list[str], str | None]:
        def _remove_term() -> tuple[bool, list[str], str | None]:
            from datetime import datetime

            term = self._sanitize_custom_moderation_term(term_raw)
            if term is None:
                return False, [], None

            db = self.client["discordguilds"]
            guilds_collection = db["guilds"]
            now = datetime.utcnow()

            result = guilds_collection.update_one(
                {"guild_id": guild_id},
                {
                    "$pull": {
                        "moderation_custom_terms": term,
                    },
                    "$set": {
                        "updated_at": now,
                    },
                },
            )

            doc = guilds_collection.find_one(
                {"guild_id": guild_id},
                {"moderation_custom_terms": 1},
            )
            raw_terms = doc.get("moderation_custom_terms") if doc else []
            normalized_terms: list[str] = []
            seen: set[str] = set()
            for raw_item in raw_terms if isinstance(raw_terms, list) else []:
                normalized = self._sanitize_custom_moderation_term(raw_item)
                if normalized is None or normalized in seen:
                    continue
                seen.add(normalized)
                normalized_terms.append(normalized)

            normalized_terms.sort()
            removed = result.modified_count > 0
            return removed, normalized_terms, term

        return await asyncio.to_thread(_remove_term)

    async def get_guild_gamification_levels(self, guild_id: int) -> list[dict[str, Any]]:
        def _get_levels():
            db = self.client["discordguilds"]
            guilds_collection = db["guilds"]
            doc = guilds_collection.find_one(
                {"guild_id": guild_id},
                {"gamification_levels": 1},
            )

            raw_levels = doc.get("gamification_levels") if doc else None
            if not isinstance(raw_levels, list) or len(raw_levels) == 0:
                return self._default_gamification_levels()

            valid_levels: list[dict[str, Any]] = []
            for row in raw_levels:
                if not isinstance(row, dict):
                    continue
                try:
                    level = int(row.get("level", 0))
                    name = str(row.get("name", "Level")).strip() or "Level"
                    interactions_required = int(row.get("interactions_required", 0))
                except (TypeError, ValueError):
                    continue

                valid_levels.append(
                    {
                        "level": level,
                        "name": name,
                        "interactions_required": max(interactions_required, 0),
                    }
                )

            if not valid_levels:
                return self._default_gamification_levels()

            valid_levels.sort(key=lambda item: item["interactions_required"])
            return valid_levels

        return await asyncio.to_thread(_get_levels)

    async def upsert_guild_gamification_level(
        self,
        guild_id: int,
        guild_name: str,
        level: int,
        level_name: str,
        interactions_required: int,
    ) -> list[dict[str, Any]]:
        def _upsert_level():
            from datetime import datetime

            db = self.client["discordguilds"]
            guilds_collection = db["guilds"]
            now = datetime.utcnow()

            doc = guilds_collection.find_one({"guild_id": guild_id}, {"gamification_levels": 1})
            existing_levels = doc.get("gamification_levels") if doc else None
            if not isinstance(existing_levels, list):
                existing_levels = self._default_gamification_levels()

            normalized: list[dict[str, Any]] = []
            for row in existing_levels:
                if not isinstance(row, dict):
                    continue
                try:
                    row_level = int(row.get("level", 0))
                    row_name = str(row.get("name", "Level")).strip() or "Level"
                    row_interactions = int(row.get("interactions_required", 0))
                except (TypeError, ValueError):
                    continue
                normalized.append(
                    {
                        "level": row_level,
                        "name": row_name,
                        "interactions_required": max(row_interactions, 0),
                    }
                )

            replaced = False
            for row in normalized:
                if row["level"] == int(level):
                    row["name"] = level_name
                    row["interactions_required"] = max(int(interactions_required), 0)
                    replaced = True
                    break

            if not replaced:
                normalized.append(
                    {
                        "level": int(level),
                        "name": level_name,
                        "interactions_required": max(int(interactions_required), 0),
                    }
                )

            normalized.sort(key=lambda item: item["interactions_required"])

            guilds_collection.update_one(
                {"guild_id": guild_id},
                {
                    "$set": {
                        "guild_id": guild_id,
                        "guild_name": guild_name,
                        "gamification_levels": normalized,
                        "updated_at": now,
                    },
                    "$setOnInsert": {
                        "created_at": now,
                        "sku": "Free",
                        "translationallowance": 500,
                        "translationcharacterallowance": 10000,
                        "aiimagegenallowance": 50,
                    },
                },
                upsert=True,
            )

            return normalized

        return await asyncio.to_thread(_upsert_level)

    async def reset_guild_gamification_levels(self, guild_id: int, guild_name: str) -> list[dict[str, Any]]:
        def _reset_levels():
            from datetime import datetime

            db = self.client["discordguilds"]
            guilds_collection = db["guilds"]
            now = datetime.utcnow()
            defaults = self._default_gamification_levels()

            guilds_collection.update_one(
                {"guild_id": guild_id},
                {
                    "$set": {
                        "guild_id": guild_id,
                        "guild_name": guild_name,
                        "gamification_levels": defaults,
                        "updated_at": now,
                    },
                    "$setOnInsert": {
                        "created_at": now,
                        "sku": "Free",
                        "translationallowance": 500,
                        "translationcharacterallowance": 10000,
                        "aiimagegenallowance": 50,
                    },
                },
                upsert=True,
            )
            return defaults

        return await asyncio.to_thread(_reset_levels)

    async def set_user_level_info(
        self,
        guild_id: int,
        user_id: int,
        level: int,
        level_name: str,
    ):
        def _set_level():
            from datetime import datetime

            db = self.client["discordguilds"]
            collection = db["user_data"]
            now = datetime.utcnow()

            collection.update_one(
                {"guild_id": guild_id, "user_id": user_id},
                {
                    "$set": {
                        "level": int(level),
                        "level_name": str(level_name),
                        "updated_at": now,
                    }
                },
                upsert=False,
            )

        await asyncio.to_thread(_set_level)

    async def recalculate_guild_user_levels(
        self,
        guild_id: int,
        level_config: list[dict[str, Any]],
    ) -> int:
        def _recalculate() -> int:
            from datetime import datetime

            db = self.client["discordguilds"]
            collection = db["user_data"]
            now = datetime.utcnow()

            normalized_levels: list[dict[str, Any]] = []
            for row in level_config:
                if not isinstance(row, dict):
                    continue
                try:
                    normalized_levels.append(
                        {
                            "level": int(row.get("level", 0)),
                            "name": str(row.get("name", "Newcomer")),
                            "interactions_required": max(int(row.get("interactions_required", 0)), 0),
                        }
                    )
                except (TypeError, ValueError):
                    continue

            if not normalized_levels:
                normalized_levels = self._default_gamification_levels()

            normalized_levels.sort(key=lambda item: item["interactions_required"])

            def _resolve_level(interactions_count: int) -> tuple[int, str]:
                current_level = 0
                current_name = "Newcomer"
                interactions = max(int(interactions_count), 0)

                for level_row in normalized_levels:
                    if interactions >= level_row["interactions_required"]:
                        current_level = int(level_row["level"])
                        current_name = str(level_row["name"])
                    else:
                        break

                return current_level, current_name

            changed_count = 0
            cursor = collection.find(
                {"guild_id": guild_id},
                {"user_id": 1, "xp": 1, "level": 1, "level_name": 1},
            )

            for doc in cursor:
                user_id = doc.get("user_id")
                if user_id is None:
                    continue

                xp_total = int(doc.get("xp", 0))
                new_level, new_level_name = _resolve_level(xp_total)
                old_level = int(doc.get("level", 0))
                old_level_name = str(doc.get("level_name", "Newcomer"))

                if new_level == old_level and new_level_name == old_level_name:
                    continue

                collection.update_one(
                    {"guild_id": guild_id, "user_id": int(user_id)},
                    {
                        "$set": {
                            "level": new_level,
                            "level_name": new_level_name,
                            "updated_at": now,
                        }
                    },
                )
                changed_count += 1

            return changed_count

        return await asyncio.to_thread(_recalculate)

    async def increment_user_moderation_flag_count(
        self,
        guild_id: int,
        guild_name: str,
        user_id: int,
        username: str,
    ) -> dict[str, int]:
        def _increment():
            from datetime import datetime

            db = self.client["discordguilds"]
            collection = db["user_data"]

            now = datetime.utcnow()
            collection.update_one(
                {"guild_id": guild_id, "user_id": user_id},
                {
                    "$set": {
                        "guild_id": guild_id,
                        "guild_name": guild_name,
                        "user_id": user_id,
                        "username": username,
                        "updated_at": now,
                    },
                    "$setOnInsert": {
                        "created_at": now,
                    },
                    "$inc": {"moderation_flag_count": 1},
                },
                upsert=True,
            )

            updated = collection.find_one(
                {"guild_id": guild_id, "user_id": user_id},
                {
                    "moderation_flag_count": 1,
                    "moderation_approved_count": 1,
                    "moderation_rejected_count": 1,
                },
            )
            if not updated:
                return {
                    "moderation_flag_count": 0,
                    "moderation_approved_count": 0,
                    "moderation_rejected_count": 0,
                }

            return {
                "moderation_flag_count": int(updated.get("moderation_flag_count", 0)),
                "moderation_approved_count": int(updated.get("moderation_approved_count", 0)),
                "moderation_rejected_count": int(updated.get("moderation_rejected_count", 0)),
            }

        return await asyncio.to_thread(_increment)

    async def increment_user_moderation_decision_count(
        self,
        guild_id: int,
        guild_name: str,
        user_id: int,
        username: str,
        approved: bool,
    ) -> dict[str, int]:
        def _increment():
            from datetime import datetime

            db = self.client["discordguilds"]
            collection = db["user_data"]
            increment_field = "moderation_approved_count" if approved else "moderation_rejected_count"

            now = datetime.utcnow()
            collection.update_one(
                {"guild_id": guild_id, "user_id": user_id},
                {
                    "$set": {
                        "guild_id": guild_id,
                        "guild_name": guild_name,
                        "user_id": user_id,
                        "username": username,
                        "updated_at": now,
                    },
                    "$setOnInsert": {
                        "created_at": now,
                    },
                    "$inc": {increment_field: 1},
                },
                upsert=True,
            )

            updated = collection.find_one(
                {"guild_id": guild_id, "user_id": user_id},
                {
                    "moderation_flag_count": 1,
                    "moderation_approved_count": 1,
                    "moderation_rejected_count": 1,
                },
            )
            if not updated:
                return {
                    "moderation_flag_count": 0,
                    "moderation_approved_count": 0,
                    "moderation_rejected_count": 0,
                }

            return {
                "moderation_flag_count": int(updated.get("moderation_flag_count", 0)),
                "moderation_approved_count": int(updated.get("moderation_approved_count", 0)),
                "moderation_rejected_count": int(updated.get("moderation_rejected_count", 0)),
            }

        return await asyncio.to_thread(_increment)

    async def increment_user_moderation_enforcement_count(
        self,
        guild_id: int,
        guild_name: str,
        user_id: int,
        username: str,
        enforcement_action: str,
    ) -> dict[str, int]:
        def _increment():
            from datetime import datetime

            field_map = {
                "kick": "moderation_kick_count",
                "ban": "moderation_ban_count",
            }
            increment_field = field_map.get(str(enforcement_action or "").strip().lower())
            if increment_field is None:
                return {
                    "moderation_flag_count": 0,
                    "moderation_approved_count": 0,
                    "moderation_rejected_count": 0,
                    "moderation_kick_count": 0,
                    "moderation_ban_count": 0,
                }

            db = self.client["discordguilds"]
            collection = db["user_data"]
            now = datetime.utcnow()

            collection.update_one(
                {"guild_id": guild_id, "user_id": user_id},
                {
                    "$set": {
                        "guild_id": guild_id,
                        "guild_name": guild_name,
                        "user_id": user_id,
                        "username": username,
                        "updated_at": now,
                    },
                    "$setOnInsert": {
                        "created_at": now,
                    },
                    "$inc": {increment_field: 1},
                },
                upsert=True,
            )

            updated = collection.find_one(
                {"guild_id": guild_id, "user_id": user_id},
                {
                    "moderation_flag_count": 1,
                    "moderation_approved_count": 1,
                    "moderation_rejected_count": 1,
                    "moderation_kick_count": 1,
                    "moderation_ban_count": 1,
                },
            )
            if not updated:
                return {
                    "moderation_flag_count": 0,
                    "moderation_approved_count": 0,
                    "moderation_rejected_count": 0,
                    "moderation_kick_count": 0,
                    "moderation_ban_count": 0,
                }

            return {
                "moderation_flag_count": int(updated.get("moderation_flag_count", 0)),
                "moderation_approved_count": int(updated.get("moderation_approved_count", 0)),
                "moderation_rejected_count": int(updated.get("moderation_rejected_count", 0)),
                "moderation_kick_count": int(updated.get("moderation_kick_count", 0)),
                "moderation_ban_count": int(updated.get("moderation_ban_count", 0)),
            }

        return await asyncio.to_thread(_increment)

    async def increment_user_xp(
        self,
        guild_id: int,
        guild_name: str,
        user_id: int,
        username: str,
        xp_gain: int,
    ) -> dict[str, int | bool]:
        def _increment():
            from datetime import datetime

            db = self.client["discordguilds"]
            collection = db["user_data"]
            now = datetime.utcnow()

            safe_gain = max(int(xp_gain), 0)

            collection.update_one(
                {"guild_id": guild_id, "user_id": user_id},
                {
                    "$set": {
                        "guild_id": guild_id,
                        "guild_name": guild_name,
                        "user_id": user_id,
                        "username": username,
                        "updated_at": now,
                    },
                    "$setOnInsert": {
                        "created_at": now,
                        "level": 0,
                        "level_name": "Newcomer",
                    },
                    "$inc": {
                        "xp": safe_gain,
                        "message_count": 1,
                    },
                },
                upsert=True,
            )

            updated = collection.find_one(
                {"guild_id": guild_id, "user_id": user_id},
                {
                    "xp": 1,
                    "level": 1,
                    "level_name": 1,
                    "message_count": 1,
                },
            )
            if not updated:
                return {
                    "xp": 0,
                    "level": 0,
                    "level_name": "Newcomer",
                    "message_count": 0,
                    "xp_gain": safe_gain,
                }

            return {
                "xp": int(updated.get("xp", 0)),
                "level": int(updated.get("level", 0)),
                "level_name": str(updated.get("level_name", "Newcomer")),
                "message_count": int(updated.get("message_count", 0)),
                "xp_gain": safe_gain,
            }

        return await asyncio.to_thread(_increment)

    async def get_user_gamification_stats(self, guild_id: int, user_id: int) -> dict[str, int] | None:
        def _get_stats():
            db = self.client["discordguilds"]
            collection = db["user_data"]

            doc = collection.find_one(
                {"guild_id": guild_id, "user_id": user_id},
                {
                    "xp": 1,
                    "level": 1,
                    "level_name": 1,
                    "message_count": 1,
                    "reputation": 1,
                    "moderation_flag_count": 1,
                    "moderation_approved_count": 1,
                    "moderation_rejected_count": 1,
                },
            )
            if not doc:
                return None

            return {
                "xp": int(doc.get("xp", 0)),
                "level": int(doc.get("level", 0)),
                "level_name": str(doc.get("level_name", "Newcomer")),
                "message_count": int(doc.get("message_count", 0)),
                "reputation": int(doc.get("reputation", 0)),
                "moderation_flag_count": int(doc.get("moderation_flag_count", 0)),
                "moderation_approved_count": int(doc.get("moderation_approved_count", 0)),
                "moderation_rejected_count": int(doc.get("moderation_rejected_count", 0)),
            }

        return await asyncio.to_thread(_get_stats)

    async def get_guild_xp_leaderboard(self, guild_id: int, limit: int = 10) -> list[dict[str, Any]]:
        def _get_leaderboard():
            db = self.client["discordguilds"]
            collection = db["user_data"]

            cursor = (
                collection.find(
                    {
                        "guild_id": guild_id,
                        "xp": {"$gt": 0},
                    },
                    {
                        "user_id": 1,
                        "username": 1,
                        "xp": 1,
                        "level": 1,
                        "level_name": 1,
                        "message_count": 1,
                    },
                )
                .sort("xp", -1)
                .limit(max(int(limit), 1))
            )

            results: list[dict[str, Any]] = []
            for doc in cursor:
                results.append(
                    {
                        "user_id": int(doc.get("user_id", 0)),
                        "username": str(doc.get("username", "Unknown")),
                        "xp": int(doc.get("xp", 0)),
                        "level": int(doc.get("level", 0)),
                        "level_name": str(doc.get("level_name", "Newcomer")),
                        "message_count": int(doc.get("message_count", 0)),
                    }
                )

            return results

        return await asyncio.to_thread(_get_leaderboard)

    async def get_guild_reputation_leaderboard(self, guild_id: int, limit: int = 10) -> list[dict[str, Any]]:
        def _get_leaderboard():
            db = self.client["discordguilds"]
            collection = db["user_data"]

            cursor = (
                collection.find(
                    {
                        "guild_id": guild_id,
                        "reputation": {"$gt": 0},
                    },
                    {
                        "user_id": 1,
                        "username": 1,
                        "reputation": 1,
                        "level": 1,
                    },
                )
                .sort("reputation", -1)
                .limit(max(int(limit), 1))
            )

            results: list[dict[str, Any]] = []
            for doc in cursor:
                results.append(
                    {
                        "user_id": int(doc.get("user_id", 0)),
                        "username": str(doc.get("username", "Unknown")),
                        "reputation": int(doc.get("reputation", 0)),
                        "level": int(doc.get("level", 0)),
                    }
                )

            return results

        return await asyncio.to_thread(_get_leaderboard)

    async def give_peer_reputation(
        self,
        guild_id: int,
        guild_name: str,
        giver_user_id: int,
        giver_username: str,
        target_user_id: int,
        target_username: str,
        cooldown_seconds: int,
    ) -> dict[str, Any]:
        def _give_reputation():
            from datetime import datetime, timedelta

            db = self.client["discordguilds"]
            collection = db["user_data"]
            now = datetime.utcnow()

            giver_doc = collection.find_one(
                {"guild_id": guild_id, "user_id": giver_user_id},
                {"rep_last_given_at": 1},
            )
            last_given_at = giver_doc.get("rep_last_given_at") if giver_doc else None

            if last_given_at:
                next_allowed_at = last_given_at + timedelta(seconds=max(int(cooldown_seconds), 0))
                if now < next_allowed_at:
                    retry_after_seconds = int((next_allowed_at - now).total_seconds())
                    return {
                        "ok": False,
                        "retry_after_seconds": max(retry_after_seconds, 1),
                    }

            collection.update_one(
                {"guild_id": guild_id, "user_id": giver_user_id},
                {
                    "$set": {
                        "guild_id": guild_id,
                        "guild_name": guild_name,
                        "user_id": giver_user_id,
                        "username": giver_username,
                        "rep_last_given_at": now,
                        "updated_at": now,
                    },
                    "$setOnInsert": {
                        "created_at": now,
                    },
                    "$inc": {
                        "rep_given_count": 1,
                    },
                },
                upsert=True,
            )

            collection.update_one(
                {"guild_id": guild_id, "user_id": target_user_id},
                {
                    "$set": {
                        "guild_id": guild_id,
                        "guild_name": guild_name,
                        "user_id": target_user_id,
                        "username": target_username,
                        "updated_at": now,
                    },
                    "$setOnInsert": {
                        "created_at": now,
                    },
                    "$inc": {
                        "reputation": 1,
                        "rep_received_count": 1,
                    },
                },
                upsert=True,
            )

            target_doc = collection.find_one(
                {"guild_id": guild_id, "user_id": target_user_id},
                {"reputation": 1},
            )
            return {
                "ok": True,
                "reputation": int(target_doc.get("reputation", 0)) if target_doc else 0,
                "retry_after_seconds": 0,
            }

        return await asyncio.to_thread(_give_reputation)

    async def adjust_user_reputation(
        self,
        guild_id: int,
        guild_name: str,
        user_id: int,
        username: str,
        delta: int,
        actor_user_id: int,
        actor_username: str,
    ) -> dict[str, int]:
        def _adjust():
            from datetime import datetime

            db = self.client["discordguilds"]
            collection = db["user_data"]
            now = datetime.utcnow()

            change = int(delta)

            collection.update_one(
                {"guild_id": guild_id, "user_id": user_id},
                {
                    "$set": {
                        "guild_id": guild_id,
                        "guild_name": guild_name,
                        "user_id": user_id,
                        "username": username,
                        "updated_at": now,
                        "rep_last_adjusted_by_user_id": actor_user_id,
                        "rep_last_adjusted_by_username": actor_username,
                    },
                    "$setOnInsert": {
                        "created_at": now,
                    },
                    "$inc": {
                        "reputation": change,
                    },
                },
                upsert=True,
            )

            doc = collection.find_one(
                {"guild_id": guild_id, "user_id": user_id},
                {"reputation": 1},
            )
            current_rep = int(doc.get("reputation", 0)) if doc else 0

            if current_rep < 0:
                collection.update_one(
                    {"guild_id": guild_id, "user_id": user_id},
                    {"$set": {"reputation": 0, "updated_at": now}},
                )
                current_rep = 0

            return {"reputation": current_rep}

        return await asyncio.to_thread(_adjust)

async def setup(bot: commands.Bot):
    await bot.add_cog(MongoDbCog(bot))
