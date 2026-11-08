from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: SecretStr
    telegram_api_base: str = ""
    telegram_local_files: bool = False
    telegram_file_root: str = ""
    telegram_mount_root: str = ""
    allowed_user_ids: str = ""
    database_url: str = "sqlite+aiosqlite:///data/bot.sqlite3"
    data_dir: Path = Path("data")
    mcp_command: str = "notebooklm-mcp"
    notebooklm_mcp_cli_path: Path | None = None
    mcp_args: list[str] = Field(default_factory=list)
    mcp_timeout_seconds: int = 240
    generation_timeout_seconds: int = 1800
    source_timeout_seconds: int = 900
    pdf_font_path: Path | None = None

    @model_validator(mode="after")
    def validate_paths(self):
        if bool(self.telegram_file_root) != bool(self.telegram_mount_root):
            raise ValueError("Set both TELEGRAM_FILE_ROOT and TELEGRAM_MOUNT_ROOT, or neither.")
        if self.telegram_local_files and not self.telegram_api_base:
            raise ValueError("Local files require TELEGRAM_API_BASE.")
        if not self.bot_token.get_secret_value():
            raise ValueError("Set BOT_TOKEN in .env.")
        self.data_dir = self.data_dir.resolve()
        return self

    def permits(self, user_id: int) -> bool:
        ids = {int(value.strip()) for value in self.allowed_user_ids.split(",") if value.strip()}
        return not ids or user_id in ids
