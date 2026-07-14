from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from pathlib import Path

# Slack notification sink for sync events. Config lives OUTSIDE the repo and
# the replica reset scope (same rationale as sync_log) so it survives full
# re-initialization; editable from the Config page via /notifications.
# Override the location with NOTIFY_CONFIG_PATH.

_LOCK = threading.Lock()
_COOLDOWN_SECONDS = 300
_LAST_SENT: dict = {}


def _path() -> Path:
    env = os.environ.get("NOTIFY_CONFIG_PATH")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".snaplicator" / "notify.json"


def get_config() -> dict:
    try:
        p = _path()
        if not p.exists():
            return {"slack_webhook_url": "", "enabled": False}
        cfg = json.loads(p.read_text(encoding="utf-8"))
        return {
            "slack_webhook_url": cfg.get("slack_webhook_url") or "",
            "enabled": bool(cfg.get("enabled")),
        }
    except Exception:
        return {"slack_webhook_url": "", "enabled": False}


def _mask(url: str) -> str:
    if not url:
        return ""
    if len(url) > 40:
        return url[:30] + "…" + url[-4:]
    return url[:10] + "…"


def get_public_config() -> dict:
    """Config shape safe to return to the UI — the URL never leaves whole."""
    cfg = get_config()
    return {
        "configured": bool(cfg["slack_webhook_url"]),
        "enabled": cfg["enabled"],
        "webhook_url_masked": _mask(cfg["slack_webhook_url"]),
    }


def set_config(webhook_url: str | None = None, enabled: bool | None = None) -> dict:
    """Partial update: None keeps the stored value, "" clears the URL."""
    with _LOCK:
        cfg = get_config()
        if webhook_url is not None:
            cfg["slack_webhook_url"] = webhook_url.strip()
        if enabled is not None:
            cfg["enabled"] = bool(enabled)
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
        try:
            p.chmod(0o600)
        except Exception:
            pass
    return get_public_config()


def send(text: str, *, kind: str = "manual", force: bool = False) -> dict:
    """POST to the Slack incoming webhook. Best-effort: never raises.

    Per-kind cooldown so the 30s loop cannot flood a channel with the same
    alert. force=True bypasses enabled/cooldown (the Config-page test button).
    """
    cfg = get_config()
    if not cfg["slack_webhook_url"]:
        return {"ok": False, "skipped": "not_configured"}
    if not force:
        if not cfg["enabled"]:
            return {"ok": False, "skipped": "disabled"}
        now = time.monotonic()
        with _LOCK:
            last = _LAST_SENT.get(kind)
            if last is not None and now - last < _COOLDOWN_SECONDS:
                return {"ok": False, "skipped": "cooldown"}
            _LAST_SENT[kind] = now
    try:
        req = urllib.request.Request(
            cfg["slack_webhook_url"],
            data=json.dumps({"text": text}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            return {"ok": 200 <= resp.status < 300, "status": resp.status}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def notify_event(kind: str, detail: dict) -> None:
    """Slack-notify sync events that carry errors; everything else is noise
    the Activity panel already shows. Same never-raise contract as sync_log."""
    try:
        err_bits = {k: v for k, v in detail.items() if "error" in k.lower() and v}
        if not err_bits:
            return
        summary = json.dumps(err_bits, ensure_ascii=False, default=str)
        if len(summary) > 500:
            summary = summary[:500] + "…"
        send(f":rotating_light: snaplicator `{kind}`\n```{summary}```", kind=kind)
    except Exception:
        pass
