from fastapi import APIRouter, HTTPException
from ...core.config import settings
from ...services.replication import get_replication_lag_seconds, get_initial_copy_progress

router = APIRouter()

@router.get("/lag")
def get_lag():
	try:
		if not settings.container_name or not settings.postgres_user or not settings.postgres_db:
			raise HTTPException(status_code=400, detail="Missing required settings (CONTAINER_NAME, POSTGRES_USER, POSTGRES_DB)")
		return get_replication_lag_seconds(settings.container_name, settings.postgres_user, settings.postgres_db)
	except HTTPException:
		raise
	except Exception as e:
		raise HTTPException(status_code=500, detail=f"Failed to compute replication lag: {e}")

@router.get("/copy-progress")
def get_copy_progress():
	try:
		if not settings.container_name or not settings.postgres_user or not settings.postgres_db:
			raise HTTPException(status_code=400, detail="Missing required settings (CONTAINER_NAME, POSTGRES_USER, POSTGRES_DB)")
		return get_initial_copy_progress(settings.container_name, settings.postgres_user, settings.postgres_db)
	except HTTPException:
		raise
	except Exception as e:
		raise HTTPException(status_code=500, detail=f"Failed to get copy progress: {e}") 