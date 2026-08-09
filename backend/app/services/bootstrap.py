"""Bringing up the replica, as an action rather than a step of the installer.

The installer used to run this itself, in one breath: create the publication,
provision the pool, start the management plane, then immediately clone the
schema and CREATE SUBSCRIPTION ... copy_data = true. That last statement is
the point of no return — once the initial copy has run there is no way back
to "not yet decided" except tearing the replica down and starting over — and
it was reached before the user had been shown a single table name. What to
replicate is the one decision that has to be made *before* the first byte
moves, and it was the one decision the flow gave no room for.

So it lives here, behind an endpoint, and the installer stops at the UI. The
work itself is unchanged: the same scripts/run-replica-postgres.sh the
installer used to exec.

The run outlives the request that starts it — an initial copy can take hours —
so it is launched in its own session with its output going to a file, and the
state is read back from disk and from the replica itself rather than held in
this process. A manager that restarts mid-copy therefore still reports the
truth, which is the whole reason none of this is kept in memory.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

from ..core.config import settings
from . import policy as policy_svc
from . import publication as publication_svc
from .replication import (
    _run_subscriber_sql,
    get_publisher_max_ddl_log_id,
    install_capture_triggers,
)

APP_DIR = Path("/app")
SCRIPT = Path("scripts/run-replica-postgres.sh")


def _state_dir() -> Path:
    # The pool, not the container: a rebuilt manager must not forget that a
    # bootstrap is running, and the pool is the one path both share.
    d = Path(settings.root_data_dir) / ".snaplicator"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _log_path() -> Path:
    return _state_dir() / "bootstrap.log"


def _pid_path() -> Path:
    return _state_dir() / "bootstrap.pid"


def _exit_path() -> Path:
    return _state_dir() / "bootstrap.exit"


def _clone_watermark_path() -> Path:
    """Where the log stands when the replica's schema is taken from it.

    The DDL stream is connected later, by the sync loop, and whatever seeds
    the watermark decides which captured DDL counts as history. Seeding it at
    connect time draws that line in the wrong place: the replica's schema is
    the primary's as of the dump, so a DDL between the dump and the connect
    is absent from the schema AND below the watermark — unreachable by
    either route, and the first write to that table then stops the apply
    worker on a column the replica has no room for.

    So the line is drawn here, where the dump happens, and carried on disk to
    whoever connects the stream.
    """
    return _state_dir() / "ddl_clone_watermark"


def clone_watermark() -> Optional[int]:
    """The log id the replica's schema was taken at, if a bootstrap set one."""
    return _read_int(_clone_watermark_path())


def _started_path() -> Path:
    # Written once, so elapsed time means time since the run began. The log's
    # mtime would answer a different question (when it last said anything),
    # which reads as a stalled run resetting its own clock.
    return _state_dir() / "bootstrap.started"


def _read_int(path: Path) -> Optional[int]:
    try:
        return int(path.read_text().strip())
    except Exception:
        return None


def _running_pid() -> Optional[int]:
    pid = _read_int(_pid_path())
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def subscription_exists() -> bool:
    """Whether the replica is already subscribed — the authoritative 'done'.

    Asked of the replica rather than of a marker file, because the marker can
    be missing (a bootstrap run before this endpoint existed, a wiped pool)
    while the subscription is plainly there.
    """
    container = settings.container_name
    user = settings.postgres_user
    db = settings.postgres_db
    if not (container and user and db):
        return False
    try:
        out = _run_subscriber_sql(
            container,
            user,
            settings.postgres_password,
            db,
            "SELECT count(*) FROM pg_subscription",
        )
    except Exception:
        return False  # container down or unreachable: not subscribed as far as we can tell
    try:
        return int(out.strip() or "0") > 0
    except ValueError:
        return False


def log_tail(lines: int = 200) -> str:
    path = _log_path()
    if not path.exists():
        return ""
    try:
        content = path.read_text(errors="replace").splitlines()
    except Exception:
        return ""
    return "\n".join(content[-lines:])


def status(tail: int = 40) -> dict:
    """running > done > failed > not_started, in that order.

    Running is checked first on purpose: a re-run over an existing replica is
    in progress, not finished, even though the subscription it will replace
    already answers yes.
    """
    pid = _running_pid()
    started_at = _read_int(_started_path())
    exit_code = _read_int(_exit_path())

    if pid:
        state = "running"
    elif subscription_exists():
        state = "done"
    elif exit_code is not None and exit_code != 0:
        state = "failed"
    elif exit_code == 0:
        # It finished and claimed success, but the subscription is not there:
        # say so rather than reporting a state that contradicts the replica.
        state = "failed"
    else:
        state = "not_started"

    return {
        "state": state,
        "pid": pid,
        "exit_code": exit_code,
        "started_at": started_at,
        "log_tail": log_tail(tail) if tail else "",
    }


def start(force: bool = False, publisher_connstr: Optional[str] = None) -> dict:
    """Launch the bootstrap. Raises RuntimeError when it would be a no-op."""
    if _running_pid():
        raise RuntimeError("a bootstrap is already running")
    if not force and subscription_exists():
        raise RuntimeError("the replica is already subscribed — pass force to run it again")

    script = APP_DIR / SCRIPT
    if not script.exists():
        raise RuntimeError(f"bootstrap script not found at {script}")

    # Capture first, then the mark, then the clone — in that order, because
    # each one only means anything if the one before it already happened.
    #
    # Capture must be running before the schema is read, or a change made
    # while the clone runs is in neither place: not in the copy that was
    # taken before it, and not in a log that was not recording yet. Nothing
    # afterwards can tell that it is missing.
    #
    # The mark then says where the clone starts from. Rows above it are what
    # the replica still owes; rows below are already in the schema it was
    # given. A primary with no log table yet has no history to skip, and 0 is
    # the right answer for that.
    if publisher_connstr:
        pub_for_capture = publication_svc.active(settings.publication_name)
        if pub_for_capture:
            try:
                install_capture_triggers(
                    publisher_connstr, pub_for_capture,
                    *policy_svc.capture_scope(pub_for_capture),
                )
            except Exception:
                # Not fatal: the loop reinstalls, and the mark below is still
                # the honest answer for whatever is being recorded now.
                pass
        try:
            _clone_watermark_path().write_text(
                str(get_publisher_max_ddl_log_id(publisher_connstr))
            )
        except Exception:
            _clone_watermark_path().write_text("0")

    log = _log_path()
    _exit_path().unlink(missing_ok=True)
    header = f"\n===== bootstrap started {time.strftime('%Y-%m-%dT%H:%M:%S%z')} =====\n"
    with log.open("a") as fh:
        fh.write(header)
        fh.flush()
        # start_new_session so the copy is not a child of this request's
        # process group: uvicorn reloading or the API being restarted must not
        # take an initial copy down with it.
        #
        # The exit code is written by the wrapper rather than collected here
        # for the same reason nothing else is kept in memory — whoever asks
        # next may be a different process than the one that started it.
        # The script reads PUBLICATION_NAME from the .env, which is the name
        # the installer proposed before it had ever looked at the primary. If
        # a publication was chosen since, that choice is the one the
        # subscription has to name — CREATE SUBSCRIPTION does not fail on a
        # publication that is not there, it succeeds and replicates nothing.
        env = dict(os.environ)
        pub = publication_svc.active(settings.publication_name)
        if pub:
            env["PUBLICATION_NAME"] = pub
        proc = subprocess.Popen(
            ["bash", "-c", f'bash "{SCRIPT}"; echo $? > "{_exit_path()}"'],
            cwd=str(APP_DIR),
            env=env,
            stdout=fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    _pid_path().write_text(str(proc.pid))
    _started_path().write_text(str(int(time.time())))
    return {"started": True, "pid": proc.pid}


def cancel() -> dict:
    """Stop a running bootstrap.

    The whole process group goes, not just the shell: the script's work is
    done by psql and docker children, and signalling only the wrapper would
    leave the copy running with nothing watching it.
    """
    pid = _running_pid()
    if not pid:
        raise RuntimeError("no bootstrap is running")
    os.killpg(os.getpgid(pid), signal.SIGTERM)
    return {"cancelled": True, "pid": pid}
