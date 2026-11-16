import contextlib
import io
import unittest

from order_simulation import (FencingTokenGenerator, OrderEvent, OrderProcessor,
                              ev)


def quiet(fn, *args):
    """Run fn without printing its log lines."""
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args)


class VersionOrderingTests(unittest.TestCase):
    def setUp(self):
        self.p = OrderProcessor()
        self.oid = "order-123"

    def test_fresh_order_starts_at_version_0(self):
        o = self.p.get_order(self.oid)
        self.assertEqual(o.current_version, 0)
        self.assertIsNone(o.current_status)

    def test_2_then_1_then_2_again(self):
        # v2 first -> buffered, not applied
        self.assertEqual(quiet(self.p.receive, ev(self.oid, 2)), "pending")
        o = self.p.get_order(self.oid)
        self.assertEqual(o.current_version, 0)
        self.assertIsNone(o.current_status)
        self.assertEqual(o.pending_versions(), [2])

        # v1 -> applied, then pending v2 drained
        self.assertEqual(quiet(self.p.receive, ev(self.oid, 1)), "applied")
        self.assertEqual(o.current_version, 2)
        self.assertEqual(o.current_status, "PAID")
        self.assertEqual(o.pending_versions(), [])

        # v2 again -> duplicate ignored, state unchanged
        self.assertEqual(quiet(self.p.receive, ev(self.oid, 2)),
                         "duplicate_ignored")
        self.assertEqual(o.current_version, 2)
        self.assertEqual(o.current_status, "PAID")

    def test_pending_drains_in_order(self):
        quiet(self.p.receive, ev(self.oid, 3))
        quiet(self.p.receive, ev(self.oid, 2))
        o = self.p.get_order(self.oid)
        self.assertEqual(o.pending_versions(), [2, 3])
        quiet(self.p.receive, ev(self.oid, 1))
        self.assertEqual(o.current_version, 3)
        self.assertEqual(o.current_status, "SHIPPED")

    def test_invalid_status_rejected(self):
        bad = OrderEvent(self.oid, 1, "SHIPPED")
        self.assertEqual(quiet(self.p.receive, bad), "rejected_invalid")
        self.assertEqual(self.p.get_order(self.oid).current_version, 0)


class MissingEventTests(unittest.TestCase):
    def test_later_version_without_earlier_stays_pending(self):
        p = OrderProcessor()
        oid = "order-missing-test"
        quiet(p.receive, ev(oid, 2))
        o = p.get_order(oid)
        self.assertEqual(o.current_version, 0)
        self.assertIsNone(o.current_status)
        self.assertEqual(o.pending_versions(), [2])


class FencingTokenTests(unittest.TestCase):
    def test_tokens_increase(self):
        g = FencingTokenGenerator()
        self.assertEqual([g.next_token() for _ in range(3)], [1, 2, 3])

    def test_stale_worker_rejected(self):
        p = OrderProcessor()
        g = FencingTokenGenerator()
        oid = "order-fencing-test"
        t1 = g.next_token()
        t2 = g.next_token()
        self.assertEqual((t1, t2), (1, 2))

        self.assertEqual(quiet(p.fenced_receive, "worker-2", t2, ev(oid, 1)),
                         "applied")
        o = p.get_order(oid)
        self.assertEqual(o.highest_fencing_token, 2)

        self.assertEqual(quiet(p.fenced_receive, "worker-1", t1, ev(oid, 2)),
                         "rejected_stale_worker")
        self.assertEqual(o.current_version, 1)
        self.assertEqual(o.current_status, "CREATED")
        self.assertEqual(o.last_writer, "worker-2")
        self.assertEqual(o.highest_fencing_token, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
