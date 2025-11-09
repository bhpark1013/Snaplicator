from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel
from ...core.config import settings
from ...services.docker_pg import delete_clone
from ...services.btrfs import list_clone_subvolumes_with_containers
from ...services.docker_pg import clone_from_main_and_run, CloneOptions, refresh_clone_in_place

router = APIRouter()

class CreateCloneBody(BaseModel):
	description: str | None = None

@router.get("")
def get_clones():
	try:
		return list_clone_subvolumes_with_containers(settings.root_data_dir, settings.main_data_dir)
	except Exception as e:
		raise HTTPException(status_code=500, detail=f"Failed to list clones: {e}")

@router.post("")
def create_clone_from_main(body: CreateCloneBody | None = None):
	try:
		required = [
			settings.container_name,
			settings.network_name,
			settings.host_port,
			settings.postgres_user,
			settings.postgres_password,
			settings.postgres_db,
		]
		if any(v in (None, "") for v in required):
			raise HTTPException(status_code=400, detail="Missing required settings in environment for clone-from-main")

		opts = CloneOptions(
			root_data_dir=settings.root_data_dir,
			main_data_dir=settings.main_data_dir,
			snapshot_name="",  # unused
			container_name=str(settings.container_name),
			network_name=str(settings.network_name),
			host_port=int(settings.host_port),
			postgres_user=str(settings.postgres_user),
			postgres_password=str(settings.postgres_password),
			postgres_db=str(settings.postgres_db),
			postgres_image=settings.postgres_image,
			description=(body.description if body else None),
		)
		return clone_from_main_and_run(opts)
	except HTTPException:
		raise
	except FileNotFoundError as e:
		raise HTTPException(status_code=404, detail=str(e))
	except Exception as e:
		raise HTTPException(status_code=500, detail=f"Failed to clone and run from main: {e}")

@router.post("/{container_name}/refresh")
def refresh_clone(
	container_name: str = Path(..., description="Docker container name of the clone to refresh"),
	body: CreateCloneBody | None = None,
):
	try:
		required = [
			settings.container_name,
			settings.network_name,
			settings.host_port,
			settings.postgres_user,
			settings.postgres_password,
			settings.postgres_db,
		]
		if any(v in (None, "") for v in required):
			raise HTTPException(status_code=400, detail="Missing required settings in environment for clone refresh")

		opts = CloneOptions(
			root_data_dir=settings.root_data_dir,
			main_data_dir=settings.main_data_dir,
			snapshot_name="",
			container_name=str(settings.container_name),
			network_name=str(settings.network_name),
			host_port=int(settings.host_port),
			postgres_user=str(settings.postgres_user),
			postgres_password=str(settings.postgres_password),
			postgres_db=str(settings.postgres_db),
			postgres_image=settings.postgres_image,
			description=(body.description if body else None),
		)
		return refresh_clone_in_place(
			container_name,
			opts,
			description_override=(body.description if body else None),
		)
	except HTTPException:
		raise
	except FileNotFoundError as e:
		raise HTTPException(status_code=404, detail=str(e))
	except RuntimeError as e:
		raise HTTPException(status_code=500, detail=str(e))
	except Exception as e:
		raise HTTPException(status_code=500, detail=f"Failed to refresh clone: {e}")

@router.delete("/{container_name}")
def remove_clone(container_name: str = Path(..., description="Docker container name of the clone")):
	try:
		if not settings.root_data_dir:
			raise HTTPException(status_code=400, detail="Missing ROOT_DATA_DIR")
		res = delete_clone(settings.root_data_dir, settings.main_data_dir, container_name)
		return res
	except HTTPException:
		raise
	except FileNotFoundError as e:
		raise HTTPException(status_code=404, detail=str(e))
	except PermissionError as e:
		raise HTTPException(status_code=403, detail=str(e))
	except Exception as e:
		raise HTTPException(status_code=500, detail=f"Failed to delete clone: {e}") 