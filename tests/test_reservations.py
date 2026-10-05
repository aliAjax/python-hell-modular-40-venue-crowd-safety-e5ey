import json
import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository, utcnow
from src.rules import RuleEngine
from src.service import DomainService


class ReservationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "reservations.db"),
            RuleEngine(),
        )
        self.coordinator = Actor("commander", "coordinator")
        self.supervisor = Actor("supervisor", "supervisor")
        self.operator_a = Actor("gate-op-a", "operator")
        self.operator_b = Actor("gate-op-b", "operator")
        self.future = "2030-01-01T00:00:00+00:00"
        self._setup_venue()

    def tearDown(self):
        self.tmp.cleanup()

    def _setup_venue(self, capacity=100):
        self.venue = self.service.create(
            self.coordinator, "venue", {"name": "V", "address": "A"}
        )
        self.zone = self.service.create(
            self.coordinator,
            "zone",
            {"venue_id": self.venue["id"], "name": "Z", "capacity": capacity},
        )
        self.gate = self.service.create(
            self.coordinator,
            "gate",
            {"venue_id": self.venue["id"], "name": "G", "zone_ids": [self.zone["id"]]},
        )
        self.service.transition(
            self.operator_a, self.gate["id"], "open", {"operator_id": "gate-op-a"}
        )
        self.service.transition(
            self.operator_a, self.zone["id"], "open", {"checklist": "clear"}
        )

    def _reserve(self, actor, count, zone_id=None, gate_id=None, expires_at=None):
        return self.service.create(
            actor,
            "reservation",
            {
                "zone_id": zone_id or self.zone["id"],
                "gate_id": gate_id or self.gate["id"],
                "count": count,
                "expires_at": expires_at or self.future,
            },
        )

    def _detail(self):
        return self.service.get(self.zone["id"])["data"]

    def _assert_reconciles(self, capacity=None):
        detail = self._detail()
        total = (
            detail["actual_occupancy"]
            + detail["reserved_count"]
            + detail["remaining_capacity"]
        )
        expected = capacity if capacity is not None else detail["capacity"]
        self.assertEqual(total, expected)

    def test_reservation_holds_quota_and_detail_reconciles(self):
        self._reserve(self.operator_a, 30)
        detail = self._detail()
        self.assertEqual(detail["actual_occupancy"], 0)
        self.assertEqual(detail["reserved_count"], 30)
        self.assertEqual(detail["remaining_capacity"], 70)
        self._assert_reconciles()

    def test_reservation_rejected_when_full(self):
        self._reserve(self.operator_a, 60)
        with self.assertRaises(ConflictError):
            self._reserve(self.operator_b, 41)
        # 60 + 40 fits exactly
        self._reserve(self.operator_b, 40)
        self._assert_reconciles()

    def test_confirm_counts_as_occupancy(self):
        reservation = self._reserve(self.operator_a, 30)
        self.service.transition(self.operator_a, reservation["id"], "confirm", {})
        detail = self._detail()
        self.assertEqual(detail["actual_occupancy"], 30)
        self.assertEqual(detail["reserved_count"], 0)
        self.assertEqual(detail["remaining_capacity"], 70)
        self._assert_reconciles()
        confirmed = self.service.get(reservation["id"])
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertIsNotNone(confirmed["data"]["confirmed_at"])

    def test_release_frees_quota_for_others(self):
        reservation = self._reserve(self.operator_a, 100)
        self.assertEqual(self._detail()["remaining_capacity"], 0)
        with self.assertRaises(ConflictError):
            self._reserve(self.operator_b, 1)
        self.service.transition(self.operator_a, reservation["id"], "release", {})
        detail = self._detail()
        self.assertEqual(detail["reserved_count"], 0)
        self.assertEqual(detail["remaining_capacity"], 100)
        # released quota is immediately usable by another gate
        self._reserve(self.operator_b, 50)
        self._assert_reconciles()

    def test_operator_cannot_release_others_reservation(self):
        reservation = self._reserve(self.operator_a, 30)
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.operator_b, reservation["id"], "release", {})
        # owner can release
        self.service.transition(self.operator_a, reservation["id"], "release", {})
        self.assertEqual(self.service.get(reservation["id"])["status"], "released")

    def test_operator_cannot_confirm_others_reservation(self):
        reservation = self._reserve(self.operator_a, 30)
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.operator_b, reservation["id"], "confirm", {})

    def test_commander_force_release_requires_reason(self):
        reservation = self._reserve(self.operator_a, 30)
        with self.assertRaises(ValidationError):
            self.service.transition(self.coordinator, reservation["id"], "force_release", {})
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.supervisor, reservation["id"], "force_release", {"reason": ""}
            )

    def test_commander_force_release_works_and_records_reason(self):
        reservation = self._reserve(self.operator_a, 30)
        self.service.transition(
            self.coordinator,
            reservation["id"],
            "force_release",
            {"reason": "commander override"},
        )
        released = self.service.get(reservation["id"])
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["data"]["release_reason"], "commander override")
        self.assertEqual(self._detail()["reserved_count"], 0)

    def test_supervisor_can_force_release(self):
        reservation = self._reserve(self.operator_a, 30)
        self.service.transition(
            self.supervisor, reservation["id"], "force_release", {"reason": "safety"}
        )
        self.assertEqual(self.service.get(reservation["id"])["status"], "released")

    def test_capacity_change_voids_unconfirmed_reservations(self):
        r1 = self._reserve(self.operator_a, 30)
        r2 = self._reserve(self.operator_b, 20)
        self.service.transition(self.operator_a, r1["id"], "confirm", {})
        # capacity shrinks to 40: r2 (reserved) is voided, r1 (confirmed) stays as occupancy
        self.service.transition(
            self.coordinator, self.zone["id"], "change_capacity", {"capacity": 40}
        )
        detail = self._detail()
        self.assertEqual(detail["actual_occupancy"], 30)
        self.assertEqual(detail["reserved_count"], 0)
        self.assertEqual(detail["remaining_capacity"], 10)
        self._assert_reconciles(capacity=40)
        self.assertEqual(self.service.get(r2["id"])["status"], "released")
        # confirmed reservation is untouched
        self.assertEqual(self.service.get(r1["id"])["status"], "confirmed")

    def test_capacity_change_rejects_non_positive(self):
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.coordinator, self.zone["id"], "change_capacity", {"capacity": 0}
            )

    def test_expired_reservation_swept_on_next_create(self):
        # insert an already-expired reserved reservation directly (time has passed)
        payload = json.dumps(
            {
                "zone_id": self.zone["id"],
                "gate_id": self.gate["id"],
                "operator_id": "gate-op-a",
                "count": 10,
                "expires_at": "2020-01-01T00:00:00+00:00",
                "confirmed_at": None,
                "released_at": None,
                "release_reason": None,
            },
            sort_keys=True,
        )
        with self.service.repository._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id,kind,status,version,data,created_by,created_at,updated_at) "
                "VALUES ('old1','reservation','reserved',1,?,'gate-op-a',?,?)",
                (payload, utcnow(), utcnow()),
            )
        self._reserve(self.operator_a, 5)
        self.assertEqual(self.service.get("old1")["status"], "expired")
        detail = self._detail()
        self.assertEqual(detail["reserved_count"], 5)
        self._assert_reconciles()

    def test_reservation_requires_future_expiry(self):
        with self.assertRaises(ValidationError):
            self._reserve(self.operator_a, 10, expires_at="2020-01-01T00:00:00+00:00")

    def test_reservation_requires_open_zone_and_gate(self):
        closed_zone = self.service.create(
            self.coordinator,
            "zone",
            {"venue_id": self.venue["id"], "name": "closed", "capacity": 100},
        )
        with self.assertRaises(ConflictError):
            self._reserve(self.operator_a, 10, zone_id=closed_zone["id"])

    def test_reservation_count_must_be_positive(self):
        with self.assertRaises(ValidationError):
            self._reserve(self.operator_a, 0)
        with self.assertRaises(ValidationError):
            self._reserve(self.operator_a, -5)

    def test_concurrent_reservations_do_not_exceed_capacity(self):
        results = {"ok": 0, "conflict": 0}
        lock = threading.Lock()

        def attempt(i):
            actor = Actor("op-%d" % i, "operator")
            try:
                self._reserve(actor, 20)
                with lock:
                    results["ok"] += 1
            except ConflictError:
                with lock:
                    results["conflict"] += 1

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(results["ok"], 5)
        self.assertEqual(results["conflict"], 5)
        detail = self._detail()
        self.assertEqual(detail["reserved_count"], 100)
        self._assert_reconciles()

    def test_reservation_idempotency(self):
        first = self.service.create(
            self.operator_a,
            "reservation",
            {
                "zone_id": self.zone["id"],
                "gate_id": self.gate["id"],
                "count": 10,
                "expires_at": self.future,
            },
            idempotency_key="res-key-1",
        )
        repeated = self.service.create(
            self.operator_a,
            "reservation",
            {
                "zone_id": self.zone["id"],
                "gate_id": self.gate["id"],
                "count": 10,
                "expires_at": self.future,
            },
            idempotency_key="res-key-1",
        )
        self.assertEqual(first["id"], repeated["id"])


if __name__ == "__main__":
    unittest.main()
