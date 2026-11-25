"""Lab 5, variant 25: structure C5 — intensity statistics aggregator with 25 buckets.

Write operation:  add(value), value is an integer 0..24.
Read operation:   snapshot() -> (25 bucket counters, count, sum, sum of squares).

The structure itself uses only plain Python lists and ints. Thread safety comes from one of
two synchronisation strategies, chosen when the aggregator is created:

  * GlobalLockAggregator — one threading.Lock; add() and snapshot() both take it for the
    whole time they touch the structure;
  * RWLockAggregator     — RWLock (below, built on threading.Condition): add() takes it in
    write mode, snapshot() in read mode.

No ready-made thread-safe collection (queue.Queue etc.) is used anywhere.

Every operation returns the moment the lock was acquired (t_acq, perf_counter_ns), so the
stand can compute t_wait = t_acq - t_call outside the structure.
"""
from __future__ import annotations

import threading
from time import perf_counter_ns

BUCKETS = 25


class _Core:
    """Unsynchronised state of the aggregator. Only add() changes fields."""

    __slots__ = ("bins", "count", "total", "total_sq", "mutations")

    def __init__(self) -> None:
        self.bins = [0] * BUCKETS
        self.count = 0
        self.total = 0
        self.total_sq = 0
        self.mutations = 0          # number of add() calls that changed the state

    def add(self, v: int) -> None:
        # bins first, count last: a snapshot that overlapped a write would see
        # sum(bins) != count, so the per-snapshot invariant also checks mutual exclusion
        self.bins[v] += 1
        self.total += v
        self.total_sq += v * v
        self.count += 1
        self.mutations += 1

    def snapshot(self) -> tuple:
        return (self.bins[:], self.count, self.total, self.total_sq)

    def state(self) -> tuple:
        return (tuple(self.bins), self.count, self.total, self.total_sq, self.mutations)


class GlobalLockAggregator:
    """Every operation takes the same mutex for its whole work with the structure."""

    name = "global"

    def __init__(self) -> None:
        self._core = _Core()
        self._lock = threading.Lock()

    def add(self, v: int) -> int:
        self._lock.acquire()
        t_acq = perf_counter_ns()
        try:
            self._core.add(v)
        finally:
            self._lock.release()
        return t_acq

    def snapshot(self) -> tuple[int, tuple]:
        self._lock.acquire()
        t_acq = perf_counter_ns()
        try:
            snap = self._core.snapshot()
        finally:
            self._lock.release()
        return t_acq, snap

    # hold-time variants for the single-thread t_cs measurement: (t_acq, t_before_release)
    def add_hold(self, v: int) -> tuple[int, int]:
        self._lock.acquire()
        t_acq = perf_counter_ns()
        try:
            self._core.add(v)
            t_end = perf_counter_ns()
        finally:
            self._lock.release()
        return t_acq, t_end

    def snapshot_hold(self) -> tuple[int, int]:
        self._lock.acquire()
        t_acq = perf_counter_ns()
        try:
            self._core.snapshot()
            t_end = perf_counter_ns()
        finally:
            self._lock.release()
        return t_acq, t_end

    def state(self) -> tuple:
        with self._lock:
            return self._core.state()


class RWLock:
    """Readers-writer lock on threading.Condition (two conditions over one mutex: readers
    wait on _rcond, writers on _wcond, so a release wakes only the side that can proceed).

    policy = "phase" (default, used for the main measurements):
      * a writer waits while another writer holds the lock, while readers hold it, and while
        a batch of readers admitted by the previous writer has not entered yet;
      * a new reader waits while a writer holds the lock or writers are waiting
        (writer preference: an endless stream of readers cannot starve a writer);
      * when a writer releases the lock, every reader that was waiting at that moment is
        admitted before the next writer (_pass), so writers cannot starve readers either.
      Writes and reads alternate in phases: one write, then the batch of readers that queued
      up during it. This is the policy of Go's sync.RWMutex.

    policy = "reader" (supplementary run): readers wait only while a writer holds the lock,
      a writer waits until there are no readers at all — reader preference, writers can
      starve while readers overlap.
    """

    def __init__(self, policy: str = "phase") -> None:
        if policy not in ("phase", "reader"):
            raise ValueError(policy)
        self.policy = policy
        self._mutex = threading.Lock()
        self._rcond = threading.Condition(self._mutex)
        self._wcond = threading.Condition(self._mutex)
        self._readers = 0           # readers inside
        self._writer = False        # a writer inside
        self._ww = 0                # writers waiting
        self._wr = 0                # readers waiting
        self._pass = 0              # readers admitted ahead of writers after the last write

    def acquire_read(self) -> None:
        with self._mutex:
            if self.policy == "reader":
                if self._writer:
                    self._wr += 1
                    while self._writer:
                        self._rcond.wait()
                    self._wr -= 1
                self._readers += 1
                return
            if not self._writer and self._ww == 0:
                self._readers += 1
                return
            self._wr += 1
            while not (self._pass > 0 or (not self._writer and self._ww == 0)):
                self._rcond.wait()
            self._wr -= 1
            if self._pass > 0:
                self._pass -= 1
            self._readers += 1

    def release_read(self) -> None:
        with self._mutex:
            self._readers -= 1
            if self._readers == 0 and self._ww > 0:
                self._wcond.notify()

    def acquire_write(self) -> None:
        with self._mutex:
            self._ww += 1
            while self._writer or self._readers > 0 or self._pass > 0:
                self._wcond.wait()
            self._ww -= 1
            self._writer = True

    def release_write(self) -> None:
        with self._mutex:
            self._writer = False
            if self._wr > 0:
                if self.policy == "phase":
                    self._pass = self._wr
                self._rcond.notify_all()
            elif self._ww > 0:
                self._wcond.notify()

    def debug_state(self) -> dict:
        with self._mutex:
            return {"readers": self._readers, "writer": self._writer, "waiting_writers": self._ww,
                    "waiting_readers": self._wr, "pass": self._pass}


class RWLockAggregator:
    """add() takes the RWLock in write mode, snapshot() in read mode."""

    name = "rw"

    def __init__(self, policy: str = "phase") -> None:
        self._core = _Core()
        self.lock = RWLock(policy)

    def add(self, v: int) -> int:
        lk = self.lock
        lk.acquire_write()
        t_acq = perf_counter_ns()
        try:
            self._core.add(v)
        finally:
            lk.release_write()
        return t_acq

    def snapshot(self) -> tuple[int, tuple]:
        lk = self.lock
        lk.acquire_read()
        t_acq = perf_counter_ns()
        try:
            snap = self._core.snapshot()
        finally:
            lk.release_read()
        return t_acq, snap

    def state(self) -> tuple:
        self.lock.acquire_read()
        try:
            return self._core.state()
        finally:
            self.lock.release_read()


def make(lock: str, policy: str = "phase"):
    if lock == "global":
        return GlobalLockAggregator()
    if lock == "rw":
        return RWLockAggregator(policy)
    raise ValueError(lock)
