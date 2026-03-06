from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TranslationData:
    discord_languages: list[dict[str, Any]]
    flag_emojis: list[str]
    discord_flag_languages: list[dict[str, Any]]
    deepl_target_languages: list[dict[str, Any]]


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as file_handle:
        return json.load(file_handle)


def _validate_records(records: list[dict[str, Any]], required_keys: set[str], label: str):
    if not isinstance(records, list):
        raise ValueError(f"{label} must be a JSON list")

    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"{label}[{index}] must be an object")
        missing = required_keys - set(record.keys())
        if missing:
            missing_display = ", ".join(sorted(missing))
            raise ValueError(f"{label}[{index}] missing required keys: {missing_display}")


def load_translation_data(data_dir: Path | None = None) -> TranslationData:
    project_root = Path(__file__).resolve().parent.parent
    base_dir = data_dir or (project_root / "data" / "translation")

    discord_languages_path = base_dir / "discord_locale_data.json"
    flag_emojis_path = base_dir / "flag_emojis_unique.json"
    discord_flag_languages_path = base_dir / "discord_flag_languages.json"
    deepl_target_languages_path = base_dir / "deepl_target_languages.json"

    for path in [
        discord_languages_path,
        flag_emojis_path,
        discord_flag_languages_path,
        deepl_target_languages_path,
    ]:
        if not path.exists():
            raise FileNotFoundError(f"Required translation data file not found: {path}")

    discord_languages = _read_json(discord_languages_path)
    flag_emojis = _read_json(flag_emojis_path)
    discord_flag_languages = _read_json(discord_flag_languages_path)
    deepl_target_languages = _read_json(deepl_target_languages_path)

    _validate_records(
        discord_languages,
        {"locale", "deepLlocale", "languageName", "nativeName", "flagEmoji"},
        "discord_locale_data.json",
    )
    if not isinstance(flag_emojis, list) or any(not isinstance(item, str) for item in flag_emojis):
        raise ValueError("flag_emojis_unique.json must be a JSON list of strings")
    _validate_records(
        discord_flag_languages,
        {"deepLlocale", "languageName", "nativeName", "flagEmoji"},
        "discord_flag_languages.json",
    )
    _validate_records(
        deepl_target_languages,
        {"language"},
        "deepl_target_languages.json",
    )

    return TranslationData(
        discord_languages=discord_languages,
        flag_emojis=flag_emojis,
        discord_flag_languages=discord_flag_languages,
        deepl_target_languages=deepl_target_languages,
    )
