from fastapi import APIRouter, HTTPException, Path
from ...core.config import settings
from ...services.docker_pg import delete_clone
from ...services.btrfs import list_clone_subvolumes_with_containers

router = APIRouter()

@router.get("")
def get_clones():
	try:
		return list_clone_subvolumes_with_containers(settings.root_data_dir, settings.main_data_dir)
	except Exception as e:
		raise HTTPException(status_code=500, detail=f"Failed to list clones: {e}")

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