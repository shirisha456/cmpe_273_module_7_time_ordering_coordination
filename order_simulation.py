"""
CMPE 273 - Module 7: Time, Ordering & Coordination

A small, deterministic, in-memory simulation of order events that can arrive
late, out of order, or more than once, plus fencing tokens that block stale
workers.

Run:  python3 order_simulation.py
"""

from dataclasses import dataclass, field
from typing import Dict, Optional

# Simple version -> status progression used by every order.
STATUS_FOR_VERSION = {1: "CREATED", 2: "PAID", 3: "SHIPPED"}


@dataclass(frozen=True)
class OrderEvent:
    """One order update. It contains exactly the three required fields."""
    order_id: str
    version: int
    status: str


@dataclass
class OrderState:
    """Everything we remember about one order (in memory only)."""
    order_id: str
    current_version: int = 0                 # new orders start at version 0
    current_status: Optional[str] = None
    pending_events: Dict[int, OrderEvent] = field(default_factory=dict)
    highest_fencing_token: int = 0           # 0 = no token accepted yet
    last_writer: Optional[str] = None        # which worker wrote last (fencing demo)

    def pending_versions(self):
        return sorted(self.pending_events)

    def describe(self):
        return (f"order={self.order_id} current_version={self.current_version} "
                f"current_status={self.current_status} "
                f"pending_versions={self.pending_versions()} "
                f"highest_fencing_token={self.highest_fencing_token} "
                f"last_writer={self.last_writer}")


def log(**fields):
    """Print one easy-to-read key=value log line."""
    print("  " + " ".join(f"{k}={v}" for k, v in fields.items()))


class OrderProcessor:
    """Applies order events strictly in version order, as they arrive."""

    def __init__(self):
        self.orders: Dict[str, OrderState] = {}

    def get_order(self, order_id: str) -> OrderState:
        if order_id not in self.orders:
            self.orders[order_id] = OrderState(order_id=order_id)
        return self.orders[order_id]

    # ------------------------------------------------------------------ #
    # Version ordering
    # ------------------------------------------------------------------ #
    def receive(self, event: OrderEvent) -> str:
        """Handle one delivered event. Returns the action taken."""
        order = self.get_order(event.order_id)
        expected = order.current_version + 1

        # Optional sanity check: status must match the version's status.
        if STATUS_FOR_VERSION.get(event.version) != event.status:
            log(order=event.order_id, received_version=event.version,
                status=event.status, action="rejected_invalid_status_for_version")
            return "rejected_invalid"

        # Case 3: duplicate or old version -> ignore, never apply twice.
        if event.version <= order.current_version:
            log(order=event.order_id, received_version=event.version,
                current_version=order.current_version,
                action="duplicate_or_old_event_ignored")
            return "duplicate_ignored"

        # Case 2: future version -> buffer it, do NOT apply yet.
        if event.version > expected:
            order.pending_events[event.version] = event
            missing = [v for v in range(expected, event.version)
                       if v not in order.pending_events]
            log(order=event.order_id, current_version=order.current_version,
                received_version=event.version, expected_version=expected,
                action="pending", missing_versions=missing,
                pending_versions=order.pending_versions())
            return "pending"

        # Case 1: exactly the next version -> apply, then drain the buffer.
        self._apply(order, event, action="applied")
        while order.current_version + 1 in order.pending_events:
            next_event = order.pending_events.pop(order.current_version + 1)
            self._apply(order, next_event, action="applied_from_pending")
        return "applied"

    def _apply(self, order: OrderState, event: OrderEvent, action: str):
        order.current_version = event.version
        order.current_status = event.status
        log(order=order.order_id, version=event.version, status=event.status,
            action=action, pending_versions=order.pending_versions())

    # ------------------------------------------------------------------ #
    # Fencing tokens
    # ------------------------------------------------------------------ #
    def fenced_receive(self, worker: str, token: int, event: OrderEvent) -> str:
        """A worker's update, guarded by its fencing token.

        Rule: reject any token OLDER than the highest token already accepted
        for this order (token < highest). The same token may be reused by the
        current lease holder for several writes; a smaller token never can.
        Only if the token passes do we run the normal version-ordering logic.
        """
        order = self.get_order(event.order_id)
        if token < order.highest_fencing_token:
            log(worker=worker, token=token,
                latest_token=order.highest_fencing_token,
                order=event.order_id, attempted_version=event.version,
                attempted_status=event.status, action="rejected_stale_worker")
            return "rejected_stale_worker"

        order.highest_fencing_token = token
        log(worker=worker, token=token, order=event.order_id, action="accepted",
            highest_fencing_token=order.highest_fencing_token)
        result = self.receive(event)
        if result == "applied":
            order.last_writer = worker
        return result


class FencingTokenGenerator:
    """In-memory monotonically increasing counter: 1, 2, 3, ...
    (Never uses timestamps.)"""

    def __init__(self):
        self._last = 0

    def next_token(self) -> int:
        self._last += 1
        return self._last


# ---------------------------------------------------------------------- #
# Demonstrations
# ---------------------------------------------------------------------- #
def ev(order_id: str, version: int) -> OrderEvent:
    return OrderEvent(order_id=order_id, version=version,
                      status=STATUS_FOR_VERSION[version])


def show_state(processor: OrderProcessor, order_id: str):
    print("  STATE: " + processor.get_order(order_id).describe())


def version_ordering_demo():
    print("=== Version Ordering Demo ===")
    p = OrderProcessor()
    oid = "order-123"
    show_state(p, oid)

    print("\n[Delivery 1] v2 PAID arrives first")
    p.receive(ev(oid, 2))
    show_state(p, oid)

    print("\n[Delivery 2] v1 CREATED arrives")
    p.receive(ev(oid, 1))
    show_state(p, oid)

    print("\n[Delivery 3] v2 PAID arrives again (duplicate)")
    p.receive(ev(oid, 2))
    show_state(p, oid)
    return p


def missing_event_demo():
    print("\n=== Missing Event Demo ===")
    p = OrderProcessor()
    oid = "order-missing-test"
    show_state(p, oid)

    print("\n[Delivery 1] only v2 PAID arrives; v1 has never arrived")
    p.receive(ev(oid, 2))
    show_state(p, oid)
    print("  RESULT: order did NOT jump to v2; it is still waiting for missing v1")

    print("\n[Recovery] v1 CREATED finally arrives")
    p.receive(ev(oid, 1))
    show_state(p, oid)
    return p


def stale_worker_demo():
    print("\n=== Stale Worker / Fencing Token Demo ===")
    p = OrderProcessor()
    tokens = FencingTokenGenerator()
    oid = "order-fencing-test"
    show_state(p, oid)

    print("\n[Step 1] worker-1 obtains a lease")
    t1 = tokens.next_token()
    log(worker="worker-1", token=t1, action="lease_granted")
    print("  (worker-1 then pauses, e.g. a long GC pause; its lease expires)")

    print("\n[Step 2] worker-2 obtains a newer lease and updates the order")
    t2 = tokens.next_token()
    log(worker="worker-2", token=t2, action="lease_granted")
    p.fenced_receive("worker-2", t2, ev(oid, 1))
    show_state(p, oid)

    print("\n[Step 3] stale worker-1 wakes up and tries to update with token 1")
    p.fenced_receive("worker-1", t1, ev(oid, 2))
    show_state(p, oid)
    print("  RESULT: stale worker-1 did NOT overwrite worker-2's state")
    return p


def main():
    version_ordering_demo()
    missing_event_demo()
    stale_worker_demo()


if __name__ == "__main__":
    main()
