from fastapi import APIRouter

from ...services.preflight import run_preflight

router = APIRouter()


@router.get("/preflight")
def preflight(deep: bool = True):
    """Pre-launch environment doctor.

    Returns a red/green checklist of every prerequisite (configs/.env,
    Docker, psql, btrfs, sudo, publisher wal_level/publication, FDW) with
    copy-paste fixes. `deep=false` skips network calls to the publisher.
    """
    return run_preflight(deep=deep)
