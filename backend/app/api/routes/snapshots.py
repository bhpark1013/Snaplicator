from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel
from ...core.config import settings
from ...services.btrfs import list_snapshots, create_snapshot
from ...services.docker_pg import clone_from_snapshot_and_run, CloneOptions
import subprocess  # type: ignore[name-defined]

router = APIRouter()

class CreateSnapshotBody(BaseModel):
	description: str | None = None

class CloneBody(BaseModel):
	description: str | None = None

@router.get("")
def get_snapshots():
	try:
		return list_snapshots(settings.root_data_dir, settings.main_data_dir)
	except FileNotFoundError as e:
		raise HTTPException(status_code=404, detail=str(e))
	except PermissionError as e:
		raise HTTPException(status_code=403, detail=str(e))
	except Exception as e:
		raise HTTPException(status_code=500, detail=f"Failed to list snapshots: {e}")


@router.post("")
def post_snapshot(body: CreateSnapshotBody | None = None):
	try:
		desc = body.description if body else None
		return create_snapshot(settings.root_data_dir, settings.main_data_dir, description=desc)
	except FileNotFoundError as e:
		raise HTTPException(status_code=404, detail=str(e))
	except FileExistsError as e:
		raise HTTPException(status_code=409, detail=str(e))
	except ValueError as e:
		raise HTTPException(status_code=400, detail=str(e))
	except PermissionError as e:
		raise HTTPException(status_code=403, detail=str(e))
	except subprocess.CalledProcessError as e:  # type: ignore[name-defined]
		# Defensive: if underlying command fails, surface stderr
		detail = e.stderr.strip() if e.stderr else str(e)
		raise HTTPException(status_code=500, detail=detail)
	except Exception as e:
		raise HTTPException(status_code=500, detail=f"Failed to create snapshot: {e}")


@router.post("/{snapshot_name}/clone")
def post_clone(
	snapshot_name: str = Path(..., description="Snapshot directory name under ROOT_DATA_DIR"),
	body: CloneBody | None = None,
):
	try:
		# Validate required settings
		required = [
			settings.container_name,
			settings.network_name,
			settings.host_port,
			settings.postgres_user,
			settings.postgres_password,
			settings.postgres_db,
		]
		if any(v in (None, "") for v in required):
			raise HTTPException(status_code=400, detail="Missing required settings in environment for clone")

		opts = CloneOptions(
			root_data_dir=settings.root_data_dir,
			main_data_dir=settings.main_data_dir,
			snapshot_name=snapshot_name,
			container_name=str(settings.container_name),
			network_name=str(settings.network_name),
			host_port=int(settings.host_port),
			postgres_user=str(settings.postgres_user),
			postgres_password=str(settings.postgres_password),
			postgres_db=str(settings.postgres_db),
			postgres_image=settings.postgres_image,
			description=(body.description if body else None),
		)
		return clone_from_snapshot_and_run(opts)
	except HTTPException:
		raise
	except FileNotFoundError as e:
		raise HTTPException(status_code=404, detail=str(e))
	except subprocess.CalledProcessError as e:  # type: ignore[name-defined]
		detail = e.stderr.strip() if e.stderr else str(e)
		raise HTTPException(status_code=500, detail=detail)
	except Exception as e:
		raise HTTPException(status_code=500, detail=f"Failed to clone and run: {e}") 