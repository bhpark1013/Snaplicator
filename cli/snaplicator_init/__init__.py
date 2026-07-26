"""snaplicator-init: survey a machine and plan where the btrfs pool should live.

Stage 1 (issue #19): read-only. Measures the replication payload, discovers
candidate locations, ranks them, and emits a plan — it never mutates anything.
"""

__version__ = "0.1.0"
