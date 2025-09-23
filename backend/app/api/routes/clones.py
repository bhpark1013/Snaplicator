from fastapi import APIRouter, HTTPException
from ...core.config import settings
from ...services.docker_pg import list_clones

router = APIRouter()

@router.get("")
def get_clones():
	try:
		return list_clones(settings.root_data_dir, settings.container_name)
	except Exception as e:
		raise HTTPException(status_code=500, detail=f"Failed to list clones: {e}") 