from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

from ...services import notify

router = APIRouter()


class NotifyConfigUpdate(BaseModel):
    webhook_url: Optional[str] = None
    enabled: Optional[bool] = None


@router.get("")
def get_notifications_config():
    return notify.get_public_config()


@router.put("")
def update_notifications_config(payload: NotifyConfigUpdate):
    return notify.set_config(webhook_url=payload.webhook_url, enabled=payload.enabled)


@router.post("/test")
def send_test_notification():
    return notify.send(
        ":white_check_mark: Snaplicator test notification — the webhook is configured correctly.",
        force=True,
    )
