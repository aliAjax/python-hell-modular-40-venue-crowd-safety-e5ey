import threading
from uuid import uuid4

from .audit import AuditTrail
from .domain import Actor, ConflictError, InvalidTransition, NotFoundError, ValidationError
from .rules import (
    RuleEngine,
    active_reserved_count,
    format_instant,
    now_utc,
    parse_instant,
    validate_reservation_cancel,
    validate_reservation_confirm,
    validate_reservation_create,
    validate_zone_capacity_change,
)

SYSTEM_ACTOR = Actor("system", "admin")
MAX_UPDATE_ATTEMPTS = 3


class DomainService:
    def __init__(self, repository, rules=None, clock=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)
        self._clock = clock or now_utc
        self._zone_locks = {}
        self._zone_locks_guard = threading.Lock()

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def _now(self):
        return self._clock()

    def _zone_lock(self, zone_id):
        # Serializes quota check-and-write per zone so concurrent gates cannot oversell it.
        with self._zone_locks_guard:
            return self._zone_locks.setdefault(zone_id, threading.Lock())

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        if kind == "reservation":
            return self._create_reservation(actor, dict(data or {}), idempotency_key)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        validated = self.rules.validate_create(actor, kind, payload, self._lookup)
        if validated:
            payload.update(validated)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] == "reservation" and action in ("confirm", "cancel"):
            return self._reservation_transition(
                actor, entity, action, dict(data or {}), expected_version
            )
        if entity["kind"] == "zone" and action == "set_capacity":
            return self._set_zone_capacity(actor, entity, dict(data or {}), expected_version)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] == "zone":
            entity["quota"] = self.zone_quota(entity)
        return entity

    def zone_quota(self, zone):
        capacity = int(zone["data"].get("capacity", 0))
        occupancy = int(zone["data"].get("current_occupancy", 0))
        reserved = self._active_reserved(zone["id"], self._now())
        return {
            "capacity": capacity,
            "current_occupancy": occupancy,
            "reserved_count": reserved,
            "remaining_capacity": capacity - occupancy - reserved,
        }

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    # --- reservation quota lifecycle -------------------------------------

    def _active_reserved(self, zone_id, now):
        return active_reserved_count(self._lookup, zone_id, now)

    def _sweep_expired(self, zone_id, now):
        """Persistently expire overdue pending reservations; released quota is
        immediately reusable because only pending rows count towards it."""
        for row in self.repository.find_entities("reservation", "zone_id", zone_id):
            if row["status"] != "pending":
                continue
            try:
                expires = parse_instant(row["data"].get("expires_at"))
            except ValidationError:
                expires = None
            if expires is not None and expires > now:
                continue
            merged = dict(row["data"])
            merged["expired_at"] = format_instant(now)
            try:
                self.repository.update_entity(row["id"], row["version"], "expired", merged)
            except ConflictError:
                continue
            self.audit.record(
                row["id"],
                SYSTEM_ACTOR,
                "expire",
                "pending",
                "expired",
                {"expires_at": row["data"].get("expires_at")},
            )

    def _create_reservation(self, actor, payload, idempotency_key):
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        zone_id = payload.get("zone_id")
        if not zone_id:
            raise ValidationError("missing required field: zone_id")
        zone = self.repository.get_entity(str(zone_id))
        if not zone or zone["kind"] != "zone":
            raise NotFoundError("zone not found: " + str(zone_id))
        gate = self.repository.get_entity(str(payload.get("gate_id") or ""))
        with self._zone_lock(zone["id"]):
            now = self._now()
            self._sweep_expired(zone["id"], now)
            validated = validate_reservation_create(
                actor, zone, gate, payload, self._active_reserved(zone["id"], now), now
            )
            payload.update(validated)
            reservation_id = str(payload.pop("id", "") or uuid4())
            if self.repository.get_entity(reservation_id):
                raise ConflictError("entity already exists: " + reservation_id)
            reservation = self.repository.create_entity(
                reservation_id, "reservation", "pending", payload, actor.user_id
            )
        self.audit.record(
            reservation_id,
            actor,
            "reserve",
            None,
            "pending",
            {
                "zone_id": zone["id"],
                "gate_id": payload.get("gate_id"),
                "count": payload.get("count"),
                "expires_at": payload.get("expires_at"),
            },
        )
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, reservation_id)
        return reservation

    def _reservation_transition(self, actor, reservation, action, data, expected_version):
        if action == "confirm":
            return self._confirm_reservation(actor, reservation, expected_version)
        return self._cancel_reservation(actor, reservation, data, expected_version)

    def _confirm_reservation(self, actor, reservation, expected_version):
        zone_id = reservation["data"].get("zone_id")
        with self._zone_lock(zone_id):
            now = self._now()
            self._sweep_expired(zone_id, now)
            current = self.repository.get_entity(reservation["id"])
            if current["status"] != "pending":
                raise InvalidTransition(
                    "cannot confirm reservation from status " + current["status"]
                )
            zone = self.repository.get_entity(zone_id)
            gate = self.repository.get_entity(current["data"].get("gate_id") or "")
            validate_reservation_confirm(actor, current, zone, gate, now)
            count = int(current["data"].get("count", 0))
            updated_zone = self._apply_confirmed_occupancy(zone_id, count)
            if updated_zone is None:
                # Failed confirmation releases the quota back to the pool.
                self._finish_reservation(
                    current,
                    actor,
                    "released",
                    {
                        "release_reason": "confirm failed: zone capacity would be exceeded",
                        "released_by": actor.user_id,
                    },
                    now,
                    "release",
                )
                raise ConflictError("zone capacity would be exceeded")
            merged = dict(current["data"])
            merged["confirmed_at"] = format_instant(now)
            merged["confirmed_by"] = actor.user_id
            updated = self.repository.update_entity(
                current["id"],
                int(expected_version) if expected_version is not None else current["version"],
                "confirmed",
                merged,
            )
            self.audit.record(
                current["id"],
                actor,
                "confirm",
                "pending",
                "confirmed",
                {"zone_id": zone_id, "count": count},
            )
            self.audit.record(
                zone_id,
                actor,
                "admit_confirmed",
                updated_zone["status"],
                updated_zone["status"],
                {
                    "reservation_id": current["id"],
                    "count": count,
                    "current_occupancy": updated_zone["data"].get("current_occupancy"),
                },
            )
            return updated

    def _apply_confirmed_occupancy(self, zone_id, count):
        for _ in range(MAX_UPDATE_ATTEMPTS):
            zone = self.repository.get_entity(zone_id)
            occupancy = int(zone["data"].get("current_occupancy", 0))
            capacity = int(zone["data"].get("capacity", 0))
            limit = capacity
            if zone["status"] == "limited":
                limit = min(limit, int(zone["data"].get("admit_limit", capacity)))
            if occupancy + count > limit:
                return None
            merged = dict(zone["data"])
            merged["current_occupancy"] = occupancy + count
            try:
                return self.repository.update_entity(
                    zone_id, zone["version"], zone["status"], merged
                )
            except ConflictError:
                continue
        raise ConflictError("zone update conflicted too many times")

    def _cancel_reservation(self, actor, reservation, data, expected_version):
        zone_id = reservation["data"].get("zone_id")
        with self._zone_lock(zone_id):
            now = self._now()
            self._sweep_expired(zone_id, now)
            current = self.repository.get_entity(reservation["id"])
            if current["status"] != "pending":
                raise InvalidTransition(
                    "cannot cancel reservation from status " + current["status"]
                )
            gate = self.repository.get_entity(current["data"].get("gate_id") or "")
            next_status, extra = validate_reservation_cancel(actor, current, gate, data)
            merged = dict(current["data"])
            merged.update(extra)
            merged["cancelled_at"] = format_instant(now)
            updated = self.repository.update_entity(
                current["id"],
                int(expected_version) if expected_version is not None else current["version"],
                next_status,
                merged,
            )
            self.audit.record(
                current["id"],
                actor,
                "revoke" if next_status == "revoked" else "release",
                "pending",
                next_status,
                dict(extra),
            )
            return updated

    def _finish_reservation(self, reservation, actor, status, extra, now, action):
        merged = dict(reservation["data"])
        merged.update(extra)
        merged["finished_at"] = format_instant(now)
        updated = self.repository.update_entity(
            reservation["id"], reservation["version"], status, merged
        )
        self.audit.record(reservation["id"], actor, action, "pending", status, dict(extra))
        return updated

    def _set_zone_capacity(self, actor, zone, data, expected_version):
        new_capacity = validate_zone_capacity_change(actor, data.get("capacity"))
        with self._zone_lock(zone["id"]):
            now = self._now()
            fresh = self.repository.get_entity(zone["id"])
            occupancy = int(fresh["data"].get("current_occupancy", 0))
            if new_capacity < occupancy:
                raise ConflictError("new capacity is below current occupancy")
            invalidated = 0
            for row in self.repository.find_entities("reservation", "zone_id", zone["id"]):
                if row["status"] != "pending":
                    continue
                extra = {
                    "invalidate_reason": "zone capacity changed",
                    "invalidated_by": actor.user_id,
                    "invalidated_at": format_instant(now),
                }
                merged = dict(row["data"])
                merged.update(extra)
                try:
                    self.repository.update_entity(row["id"], row["version"], "invalidated", merged)
                except ConflictError:
                    continue
                self.audit.record(row["id"], actor, "invalidate", "pending", "invalidated", dict(extra))
                invalidated += 1
            merged_zone = dict(fresh["data"])
            merged_zone["capacity"] = new_capacity
            updated = self.repository.update_entity(
                zone["id"],
                int(expected_version) if expected_version is not None else fresh["version"],
                fresh["status"],
                merged_zone,
            )
            self.audit.record(
                zone["id"],
                actor,
                "set_capacity",
                fresh["status"],
                updated["status"],
                {
                    "from_capacity": fresh["data"].get("capacity"),
                    "to_capacity": new_capacity,
                    "invalidated_reservations": invalidated,
                },
            )
            return updated
