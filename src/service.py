from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError, PermissionDenied, ValidationError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        if kind == "reservation":
            return self._create_reservation(actor, data, idempotency_key)
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
        if entity["kind"] == "reservation":
            return self._reservation_action(actor, entity, action, data or {})
        if entity["kind"] == "zone" and action == "change_capacity":
            return self._change_zone_capacity(actor, entity, data or {})
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
            detail = self.repository.get_zone_detail(entity_id)
            if detail:
                entity = detail
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    def _create_reservation(self, actor, data, idempotency_key):
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        validated = self.rules.validate_create(actor, "reservation", payload, self._lookup)
        if validated:
            payload.update(validated)
        reservation_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(reservation_id):
            raise ConflictError("entity already exists: " + reservation_id)
        reservation = self.repository.create_reservation(
            reservation_id,
            payload["zone_id"],
            payload["gate_id"],
            actor.user_id,
            payload["count"],
            payload["expires_at"],
        )
        self.audit.record(
            reservation_id, actor, "create", None, "reserved", {"kind": "reservation"}
        )
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, reservation_id)
        return reservation

    def _reservation_action(self, actor, entity, action, data):
        if action == "confirm":
            return self._confirm_reservation(actor, entity, data)
        if action == "release":
            return self._release_reservation(actor, entity, data, force=False)
        if action == "force_release":
            return self._release_reservation(actor, entity, data, force=True)
        raise InvalidTransition("unknown action %s for reservation" % action)

    def _confirm_reservation(self, actor, entity, data):
        self.rules.validate_transition(actor, entity, "confirm", dict(data or {}), self._lookup)
        if actor.role == "operator" and entity["data"].get("operator_id") != actor.user_id:
            raise PermissionDenied("operators can only confirm their own reservations")
        reservation, zone = self.repository.confirm_reservation(entity["id"])
        self.audit.record(
            entity["id"], actor, "confirm", entity["status"], "confirmed",
            {"count": entity["data"].get("count")},
        )
        self.audit.record(
            zone["id"], actor, "confirm", zone["status"], zone["status"],
            {"reservation_id": entity["id"], "count": entity["data"].get("count")},
        )
        return reservation

    def _release_reservation(self, actor, entity, data, force):
        action = "force_release" if force else "release"
        self.rules.validate_transition(actor, entity, action, dict(data or {}), self._lookup)
        if not force and entity["data"].get("operator_id") != actor.user_id:
            raise PermissionDenied("operators can only release their own reservations")
        reason = (data or {}).get("reason")
        reservation = self.repository.release_reservation(entity["id"], reason)
        self.audit.record(
            entity["id"], actor, action, entity["status"], "released", {"reason": reason}
        )
        return reservation

    def _change_zone_capacity(self, actor, entity, data):
        if actor.role not in ("coordinator", "supervisor", "admin"):
            raise PermissionDenied("role %s is not allowed here" % actor.role)
        payload = dict(data or {})
        try:
            capacity = int(payload.get("capacity"))
        except (TypeError, ValueError):
            raise ValidationError("capacity must be an integer")
        if capacity <= 0:
            raise ValidationError("capacity must be positive")
        zone = self.repository.change_zone_capacity(entity["id"], capacity)
        self.audit.record(
            entity["id"], actor, "change_capacity", entity["status"], zone["status"],
            {"capacity": capacity},
        )
        return self.get(entity["id"])
