"""What the record says while a clone is being built.

The work reports one stage at a time and never brackets them, so everything
else — which stages finished, which were never entered, how long each took —
is derived on the way past. These pin that derivation, because a wrong answer
here is a screen that says a clone is doing something it finished minutes ago.
"""
from __future__ import annotations

from app.services import clone_progress


def status_of(rec, key):
    return next(s["status"] for s in rec["stages"] if s["key"] == key)


def test_nothing_has_run_yet():
    clone_progress._state = None
    assert clone_progress.current() is None


def test_moving_on_settles_what_came_before():
    clone_progress.begin("create", "nightly")
    clone_progress.stage("snapshot")
    rec = clone_progress.current()
    assert status_of(rec, "snapshot") == "running"
    # Entered without checkpoint ever being reported, so it was not entered.
    assert status_of(rec, "checkpoint") == "skipped"
    assert status_of(rec, "container") == "pending", "still ahead of us"

    clone_progress.stage("container")
    rec = clone_progress.current()
    assert status_of(rec, "snapshot") == "done"
    assert rec["stages"][1]["ms"] is not None, "and timed"
    assert status_of(rec, "container") == "running"
    assert rec["stage"] == "container"
    assert rec["active"] is True


def test_finishing_clears_the_running_stage_and_writes_off_the_rest():
    clone_progress.begin("create")
    clone_progress.stage("anonymize")
    clone_progress.finish()

    rec = clone_progress.current()
    assert rec["active"] is False
    assert rec["error"] is None
    assert status_of(rec, "anonymize") == "done"
    # It ended without complaint, so what was never entered was not needed.
    assert status_of(rec, "user") == "skipped"


def test_a_failure_marks_the_stage_it_failed_in():
    clone_progress.begin("create")
    clone_progress.stage("ready")
    clone_progress.finish(error="container never answered")

    rec = clone_progress.current()
    assert rec["active"] is False
    assert rec["error"] == "container never answered"
    assert status_of(rec, "ready") == "failed"
    # Unreached, not unnecessary — saying "skipped" would claim they were
    # considered and declined.
    assert status_of(rec, "anonymize") == "pending"


def test_reporting_into_a_finished_record_changes_nothing():
    """A late report must not resurrect the run it belongs to.

    Stage reports come from inside the work, and the work's own cleanup can
    outlive the failure that ended it.
    """
    clone_progress.begin("create")
    clone_progress.stage("snapshot")
    clone_progress.finish(error="boom")
    clone_progress.stage("anonymize")

    rec = clone_progress.current()
    assert rec["active"] is False
    assert rec["stage"] == "snapshot"
    assert status_of(rec, "anonymize") == "pending"


def test_the_internal_bookkeeping_never_leaves_the_module():
    """The timing marks and the owning thread id go over the wire otherwise."""
    clone_progress.begin("create")
    clone_progress.stage("snapshot")
    rec = clone_progress.current()
    assert all(not k.startswith("_") for k in rec)
    assert all(not k.startswith("_") for s in rec["stages"] for k in s)


def test_beginning_again_discards_the_previous_run():
    clone_progress.begin("create", "first")
    clone_progress.stage("anonymize")
    clone_progress.begin("create", "second")

    rec = clone_progress.current()
    assert rec["name"] == "second"
    assert rec["stage"] is None
    assert {s["status"] for s in rec["stages"]} == {"pending"}


def test_another_operation_cannot_file_stages_under_this_one():
    """Refresh and reset launch containers through the same code as a build.

    The API serves each request on its own thread, so without an owner a
    refresh started elsewhere would report its stages into the build this
    screen is watching — and the screen would say the build had moved on.
    """
    import threading

    clone_progress.begin("create", "watched")
    clone_progress.stage("snapshot")

    def intruder():
        clone_progress.stage("anonymize")
        clone_progress.finish(error="not mine to end")

    t = threading.Thread(target=intruder)
    t.start()
    t.join()

    rec = clone_progress.current()
    assert rec["stage"] == "snapshot", "the other thread's report was dropped"
    assert rec["active"] is True, "and it could not end this run either"
    assert rec["error"] is None
