import threading
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine, format_instant, now_utc
from src.service import DomainService


class ReservationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = [now_utc()]
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "reservations.db"),
            RuleEngine(),
            clock=lambda: self.now[0],
        )
        self.coordinator = Actor("coordinator", "coordinator")
        self.supervisor = Actor("supervisor", "supervisor")
        self.operator_a = Actor("operator-a", "operator")
        self.operator_b = Actor("operator-b", "operator")
        self.venue = self.service.create(
            self.coordinator, "venue", {"name": "V", "address": "A"}
        )
        self.zone = self.service.create(
            self.coordinator,
            "zone",
            {"venue_id": self.venue["id"], "name": "Z", "capacity": 100},
        )
        self.gate_a = self._gate("Gate A", "operator-a")
        self.gate_b = self._gate("Gate B", "operator-b")
        self.service.transition(self.operator_a, self.zone["id"], "open", {})

    def tearDown(self):
        self.tmp.cleanup()

    def _gate(self, name, operator_id):
        gate = self.service.create(
            self.coordinator,
            "gate",
            {"venue_id": self.venue["id"], "name": name, "zone_ids": [self.zone["id"]]},
        )
        return self.service.transition(
            Actor(operator_id, "operator"), gate["id"], "open", {"operator_id": operator_id}
        )

    def _expires(self, seconds=300):
        return format_instant(self.now[0] + timedelta(seconds=seconds))

    def _reserve(self, actor, gate, count, seconds=300, idempotency_key=None):
        return self.service.create(
            actor,
            "reservation",
            {
                "zone_id": self.zone["id"],
                "gate_id": gate["id"],
                "count": count,
                "expires_at": self._expires(seconds),
            },
            idempotency_key,
        )

    def _quota(self):
        return self.service.get(self.zone["id"])["quota"]

    def _assert_quota_consistent(self):
        quota = self._quota()
        self.assertEqual(
            quota["capacity"],
            quota["current_occupancy"]
            + quota["reserved_count"]
            + quota["remaining_capacity"],
        )
        return quota

    def test_reserve_confirm_and_quota_consistency(self):
        reservation = self._reserve(self.operator_a, self.gate_a, 30)
        self.assertEqual(reservation["status"], "pending")
        self.assertEqual(reservation["data"]["expires_at"], self._expires())
        quota = self._assert_quota_consistent()
        self.assertEqual(
            (quota["current_occupancy"], quota["reserved_count"], quota["remaining_capacity"]),
            (0, 30, 70),
        )

        confirmed = self.service.transition(self.operator_a, reservation["id"], "confirm", {})
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["data"]["confirmed_by"], "operator-a")
        quota = self._assert_quota_consistent()
        self.assertEqual(
            (quota["current_occupancy"], quota["reserved_count"], quota["remaining_capacity"]),
            (30, 0, 70),
        )
        actions = [row["action"] for row in self.service.audit_log(reservation["id"])]
        self.assertEqual(actions, ["reserve", "confirm"])

    def test_reserve_validation_and_capacity_rejection(self):
        with self.assertRaises(ValidationError):
            self._reserve(self.operator_a, self.gate_a, 0)
        with self.assertRaises(ValidationError):
            self.service.create(
                self.operator_a,
                "reservation",
                {"zone_id": self.zone["id"], "gate_id": self.gate_a["id"], "count": 5},
            )
        with self.assertRaises(ValidationError):
            self._reserve(self.operator_a, self.gate_a, 5, seconds=-1)
        self._reserve(self.operator_a, self.gate_a, 80)
        with self.assertRaises(ConflictError):
            self._reserve(self.operator_b, self.gate_b, 21)
        quota = self._assert_quota_consistent()
        self.assertEqual(quota["reserved_count"], 80)

    def test_reserve_idempotency_key(self):
        first = self._reserve(self.operator_a, self.gate_a, 10, idempotency_key="k-1")
        second = self._reserve(self.operator_a, self.gate_a, 10, idempotency_key="k-1")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self._quota()["reserved_count"], 10)

    def test_expiry_releases_quota_for_other_gates(self):
        reservation = self._reserve(self.operator_a, self.gate_a, 90, seconds=30)
        with self.assertRaises(ConflictError):
            self._reserve(self.operator_b, self.gate_b, 90)
        self.now[0] += timedelta(seconds=31)
        other = self._reserve(self.operator_b, self.gate_b, 90)
        self.assertEqual(other["status"], "pending")
        expired = self.service.get(reservation["id"])
        self.assertEqual(expired["status"], "expired")
        quota = self._assert_quota_consistent()
        self.assertEqual(
            (quota["reserved_count"], quota["remaining_capacity"]), (90, 10)
        )

    def test_confirm_after_expiry_fails(self):
        reservation = self._reserve(self.operator_a, self.gate_a, 10, seconds=30)
        self.now[0] += timedelta(seconds=31)
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.operator_a, reservation["id"], "confirm", {})
        self.assertEqual(self.service.get(reservation["id"])["status"], "expired")
        self.assertEqual(self._assert_quota_consistent()["reserved_count"], 0)

    def test_confirm_failure_releases_reservation(self):
        reservation = self._reserve(self.operator_a, self.gate_a, 60)
        self.service.transition(
            self.supervisor,
            self.zone["id"],
            "restrict",
            {"reason": "crowd control", "admit_limit": 50},
        )
        with self.assertRaises(ConflictError):
            self.service.transition(self.operator_a, reservation["id"], "confirm", {})
        released = self.service.get(reservation["id"])
        self.assertEqual(released["status"], "released")
        self.assertIn("confirm failed", released["data"]["release_reason"])
        self.assertEqual(self._assert_quota_consistent()["reserved_count"], 0)

    def test_operator_cannot_cancel_other_gate_and_release_frees_quota(self):
        reservation = self._reserve(self.operator_a, self.gate_a, 90)
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.operator_b, reservation["id"], "cancel", {})
        with self.assertRaises(ConflictError):
            self._reserve(self.operator_b, self.gate_b, 90)
        cancelled = self.service.transition(self.operator_a, reservation["id"], "cancel", {})
        self.assertEqual(cancelled["status"], "released")
        replacement = self._reserve(self.operator_b, self.gate_b, 90)
        self.assertEqual(replacement["status"], "pending")
        self.assertEqual(self._assert_quota_consistent()["reserved_count"], 90)

    def test_force_revoke_requires_reason(self):
        reservation = self._reserve(self.operator_a, self.gate_a, 40)
        with self.assertRaises(ValidationError):
            self.service.transition(self.coordinator, reservation["id"], "cancel", {})
        revoked = self.service.transition(
            self.coordinator,
            reservation["id"],
            "cancel",
            {"reason": "VIP convoy rerouting"},
        )
        self.assertEqual(revoked["status"], "revoked")
        self.assertEqual(revoked["data"]["revoke_reason"], "VIP convoy rerouting")
        self.assertEqual(revoked["data"]["revoked_by"], "coordinator")
        audit = self.service.audit_log(reservation["id"])
        self.assertEqual(audit[-1]["action"], "revoke")
        self.assertEqual(audit[-1]["detail"]["revoke_reason"], "VIP convoy rerouting")
        self.assertEqual(self._assert_quota_consistent()["reserved_count"], 0)

    def test_capacity_change_invalidates_pending_reservations(self):
        pending = self._reserve(self.operator_a, self.gate_a, 40)
        confirmed = self._reserve(self.operator_b, self.gate_b, 20)
        self.service.transition(self.operator_b, confirmed["id"], "confirm", {})
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.operator_a, self.zone["id"], "set_capacity", {"capacity": 80}
            )
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.coordinator, self.zone["id"], "set_capacity", {"capacity": 10}
            )
        updated = self.service.transition(
            self.coordinator, self.zone["id"], "set_capacity", {"capacity": 50}
        )
        self.assertEqual(updated["data"]["capacity"], 50)
        self.assertEqual(self.service.get(pending["id"])["status"], "invalidated")
        self.assertEqual(self.service.get(confirmed["id"])["status"], "confirmed")
        quota = self._assert_quota_consistent()
        self.assertEqual(
            (
                quota["capacity"],
                quota["current_occupancy"],
                quota["reserved_count"],
                quota["remaining_capacity"],
            ),
            (50, 20, 0, 30),
        )

    def test_concurrent_reservations_never_oversell(self):
        gates = [self.gate_a, self.gate_b]
        results = []
        errors = []

        def attempt(index):
            try:
                results.append(self._reserve(self.supervisor, gates[index % 2], 15))
            except ConflictError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 6)
        self.assertEqual(len(errors), 2)
        quota = self._assert_quota_consistent()
        self.assertEqual(
            (quota["reserved_count"], quota["remaining_capacity"]), (90, 10)
        )

    def test_admit_respects_reserved_quota(self):
        self._reserve(self.operator_a, self.gate_a, 60)
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.operator_b,
                self.zone["id"],
                "admit",
                {"gate_id": self.gate_b["id"], "count": 41, "admitted_at": "t1"},
            )
        zone = self.service.transition(
            self.operator_b,
            self.zone["id"],
            "admit",
            {"gate_id": self.gate_b["id"], "count": 40, "admitted_at": "t2"},
        )
        self.assertEqual(zone["data"]["current_occupancy"], 40)
        quota = self._assert_quota_consistent()
        self.assertEqual(
            (quota["current_occupancy"], quota["reserved_count"], quota["remaining_capacity"]),
            (40, 60, 0),
        )


if __name__ == "__main__":
    unittest.main()
