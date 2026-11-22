from pathlib import Path as FsPath

from fastapi import APIRouter, HTTPException

from ...core.config import settings
from ...services import divergence as divergence_svc
from ...services.btrfs import _get_fs_totals_bytes
from ...services.divergence import build_response, live_name, read_labels

router = APIRouter()


@router.get("/disk")
def get_disk_usage():
	"""Who holds the pool's blocks, from the last background measurement.
	Never measures inline; starts a pass when the cache is missing or old."""
	try:
		live = live_name(settings.root_data_dir, settings.main_data_dir)
		data = divergence_svc.load_cached()
		if divergence_svc.is_stale(data):
			divergence_svc.start_refresh(settings.root_data_dir, live)
		size, used = _get_fs_totals_bytes(FsPath(settings.root_data_dir))
		labels = read_labels(settings.root_data_dir, (data or {}).get("subvolumes", {}).keys())
		return build_response(data, live, size, used, divergence_svc.status(), labels)
	except Exception as e:
		raise HTTPException(status_code=500, detail=f"Failed to read disk usage: {e}")


@router.post("/disk/refresh")
def refresh_disk_usage():
	live = live_name(settings.root_data_dir, settings.main_data_dir)
	started = divergence_svc.start_refresh(settings.root_data_dir, live)
	return {"started": started, **divergence_svc.status()}
