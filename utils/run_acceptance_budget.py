"""Thread-safe run-level limits for newly persisted discovery records."""

from threading import Lock


class AcceptanceReservation:
    """One provisional slot in a :class:`RunAcceptanceBudget`."""

    def __init__(self, budget):
        self._budget = budget
        self._state = "reserved"

    def commit(self):
        """Mark this slot as durably persisted."""
        return self._budget._transition(self, "committed")

    def release(self):
        """Return this slot after an unsuccessful acceptance attempt."""
        return self._budget._transition(self, "released")


class RunAcceptanceBudget:
    """Bound accepted records across every query and worker in one run."""

    def __init__(self, limit, *, committed=0):
        if limit is not None and limit < 1:
            raise ValueError("limit must be >= 1 when provided")
        if committed < 0 or (limit is not None and committed > limit):
            raise ValueError("committed must be between zero and limit")
        self.limit = limit
        self._reserved = 0
        self._committed = committed
        self._lock = Lock()

    def try_reserve(self):
        """Atomically reserve capacity, returning ``None`` when exhausted."""
        with self._lock:
            if (
                self.limit is not None
                and self._reserved + self._committed >= self.limit
            ):
                return None
            self._reserved += 1
            return AcceptanceReservation(self)

    def _transition(self, reservation, target):
        with self._lock:
            if reservation._budget is not self or reservation._state != "reserved":
                return False
            self._reserved -= 1
            if target == "committed":
                self._committed += 1
            reservation._state = target
            return True

    def exhausted(self):
        """Return whether every available slot is committed or reserved."""
        with self._lock:
            return (
                self.limit is not None
                and self._reserved + self._committed >= self.limit
            )

    def committed_exhausted(self):
        """Return whether durable accepts alone have reached the limit."""
        with self._lock:
            return self.limit is not None and self._committed >= self.limit

    def snapshot(self):
        with self._lock:
            remaining = (
                None if self.limit is None
                else max(self.limit - self._reserved - self._committed, 0)
            )
            return {
                "limit": self.limit,
                "reserved": self._reserved,
                "committed": self._committed,
                "remaining": remaining,
            }
