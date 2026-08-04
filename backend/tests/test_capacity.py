"""What the copy refuses over, and what it only mentions.

The installer has to guess: it runs before anything is selected, so its only
honest scope is the whole database. Refusing there turned the largest possible
choice into a gate on every smaller one — a 344 GiB database would not install
on 594 GiB of free disk, though the data fits with room to spare and
provisioning reserves nothing.

So the question is asked again here, where the selection exists, and split in
two: whether the data can land at all, and whether room is left over
afterwards. Only the first is a fact.
"""
from __future__ import annotations

from app.services import capacity

GiB = 1024 ** 3


def result(payload, free):
    """A check() result, without a database or a disk to get one from."""
    minimum = int(capacity.MINIMUM_MULTIPLIER * payload)
    roomy = capacity.ROOMY_MULTIPLIER * payload
    return {
        "pool": "/snaplicator",
        "payload_bytes": payload,
        "free_bytes": free,
        "minimum_bytes": minimum,
        "roomy_bytes": roomy,
        "fits": None if free is None or payload <= 0 else free >= minimum,
        "comfortable": None if free is None or payload <= 0 else free >= roomy,
    }


class TestRefusal:
    def test_the_case_the_installer_used_to_refuse(self):
        """344 GiB of tables, 594 GiB free — the run this was changed for."""
        r = result(344 * GiB, 594 * GiB)
        assert r["fits"] is True
        assert r["comfortable"] is True, "594 clears 344 × 1.5"
        assert capacity.refusal(r) is None

    def test_between_the_two_marks_is_a_remark_not_a_refusal(self):
        r = result(344 * GiB, 400 * GiB)
        assert r["fits"] is True
        assert r["comfortable"] is False
        assert capacity.refusal(r) is None, "tight is not a reason to refuse"

    def test_a_copy_that_cannot_finish_is_refused(self):
        r = result(344 * GiB, 100 * GiB)
        assert r["fits"] is False
        why = capacity.refusal(r)
        assert why is not None
        assert "344.0 GiB" in why and "100.0 GiB" in why, "the numbers, not just a verdict"
        assert "fewer tables" in why, "and a way out"

    def test_room_to_spare_says_nothing(self):
        r = result(100 * GiB, 900 * GiB)
        assert (r["fits"], r["comfortable"]) == (True, True)
        assert capacity.refusal(r) is None

    def test_an_unreadable_pool_does_not_refuse(self):
        """A check that cannot see is not entitled to stop anything."""
        r = result(344 * GiB, None)
        assert r["fits"] is None
        assert capacity.refusal(r) is None

    def test_an_empty_selection_does_not_refuse(self):
        r = result(0, 10 * GiB)
        assert r["fits"] is None
        assert capacity.refusal(r) is None

    def test_the_floor_is_above_the_payload(self):
        """Exactly payload-sized is not enough — postgres needs slack to run."""
        payload = 100 * GiB
        assert result(payload, payload)["fits"] is False
        assert result(payload, int(payload * 1.1))["fits"] is True


class TestPoolFreeBytes:
    def test_a_missing_pool_reads_as_unknown_not_as_zero(self, monkeypatch):
        """Zero would refuse every copy; unknown refuses none."""
        monkeypatch.setattr(capacity, "pool_dir", lambda: "/nonexistent-pool-xyz")
        assert capacity.pool_free_bytes() is None

    def test_a_real_directory_reports_something(self, monkeypatch, tmp_path):
        monkeypatch.setattr(capacity, "pool_dir", lambda: str(tmp_path))
        free = capacity.pool_free_bytes()
        assert free is not None and free > 0
