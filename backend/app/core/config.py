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

	# Publisher connection (libpq connstr, e.g. "host=... port=... user=... password=... dbname=...")
	publisher_connstr: Optional[str] = None

	# Fallback fields to build publisher connstr like scripts do
	primary_host: Optional[str] = None
	primary_port: Optional[int] = None
	primary_db: Optional[str] = None
	primary_user: Optional[str] = None
	primary_password: Optional[str] = None
	pgsslmode: Optional[str] = None  # e.g., require/prefer

	# Publication & Subscription names
	publication_name: Optional[str] = None
	subscription_name: Optional[str] = None
	ddl_sync_interval: Optional[int] = 30  # seconds, 0 to disable
	replication_schemas: Optional[str] = None  # comma-separated schemas to monitor, e.g. "public,deprecated,etl"

	# FDW (postgres_fdw) — credentials and (optionally) a different connection target
	# than logical replication. Often FDW goes through a bastion/pgbouncer with a
	# read-only role, while replication connects directly to the primary cluster
	# with a replication-privileged role. If FDW_HOST/PORT/DB are unset they fall
	# back to PRIMARY_HOST/PORT/DB.
	fdw_user: Optional[str] = None
	fdw_password: Optional[str] = None
	fdw_host: Optional[str] = None
	fdw_port: Optional[int] = None
	fdw_db: Optional[str] = None

	# Paths are relative to repo root unless absolute. The yaml is the single source
	# of truth; the .generated.sql file is rendered from it.
	fdw_yaml_path: str = "configs/fdw.yaml"
	fdw_sql_path: str = "configs/fdw_setup.generated.sql"

	model_config = SettingsConfigDict(env_file=None, extra="ignore")

	def fdw_yaml_abs(self) -> Path:
		p = Path(self.fdw_yaml_path)
		return p if p.is_absolute() else _REPO_ROOT / p

	def fdw_sql_abs(self) -> Path:
		p = Path(self.fdw_sql_path)
		return p if p.is_absolute() else _REPO_ROOT / p

	def effective_fdw_host(self) -> Optional[str]:
		return self.fdw_host or self.primary_host

	def effective_fdw_port(self) -> Optional[int]:
		return self.fdw_port or self.primary_port

	def effective_fdw_db(self) -> Optional[str]:
		return self.fdw_db or self.primary_db

settings = Settings()
