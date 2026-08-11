from fastapi import APIRouter, HTTPException, Body
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from ...services import anonymize as anon_svc

router = APIRouter()


class AnonymizeSqlBody(BaseModel):
    content: str


@router.get("")
def get_anonymize_sql():
    """Current anonymization script plus an advisory check of the tables it
    writes to (a missing one aborts clone creation from main)."""
    try:
        data = anon_svc.get_content()
        data.update(anon_svc.validate(data["content"]) if data["content"] else
                    {"referenced_tables": [], "missing_tables": [], "checked": False, "warnings": []})
        data["backups"] = anon_svc.list_backups()
        data["readiness"] = anon_svc.readiness()
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read anonymize.sql: {e}")


@router.put("")
def put_anonymize_sql(body: AnonymizeSqlBody = Body(...)):
    """Replace the script. The previous version is kept as a timestamped
    backup next to it. Warnings are advisory — the save always applies."""
    try:
        return anon_svc.save_content(body.content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save anonymize.sql: {e}")


@router.get("/download", response_class=PlainTextResponse)
def download_anonymize_sql():
    try:
        data = anon_svc.get_content()
        if not data["exists"]:
            raise HTTPException(status_code=404, detail="anonymize.sql does not exist")
        return PlainTextResponse(
            data["content"],
            media_type="application/sql",
            headers={"Content-Disposition": 'attachment; filename="anonymize.sql"'},
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to download anonymize.sql: {e}")
