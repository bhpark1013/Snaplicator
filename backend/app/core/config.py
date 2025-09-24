from __future__ import annotations

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional, List
from dotenv import load_dotenv

# Load .env from repository root (Snaplicator/configs/.env)
_REPO_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(_REPO_ROOT / "configs/.env", override=False)

class Settings(BaseSettings):
	root_data_dir: str
	main_data_dir: str
	allow_origins: List[str] = ["*"]

	# Optional for clone/container API
	container_name: Optional[str] = None
	network_name: Optional[str] = None
	host_port: Optional[int] = None
	postgres_user: Optional[str] = None
	postgres_password: Optional[str] = None
	postgres_db: Optional[str] = None
	postgres_image: str = "postgres:17"

	model_config = SettingsConfigDict(env_file=None, extra="ignore")

settings = Settings() 