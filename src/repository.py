import json
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from .domain import ConflictError, InvalidTransition, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def create_reservation(self, reservation_id, zone_id, gate_id, operator_id, count, expires_at):
        now = utcnow()
        payload = json.dumps(
            {
                "zone_id": zone_id,
                "gate_id": gate_id,
                "operator_id": operator_id,
                "count": int(count),
                "expires_at": expires_at,
                "confirmed_at": None,
                "released_at": None,
                "release_reason": None,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, status, data FROM entities WHERE id = ?", (zone_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("zone not found: " + zone_id)
            if row["status"] not in ("open", "limited"):
                raise ConflictError("zone is not accepting reservations")
            zone_data = json.loads(row["data"])
            capacity = int(zone_data.get("capacity", 0))
            occupancy = int(zone_data.get("current_occupancy", 0))
            connection.execute(
                "UPDATE entities SET status = 'expired', updated_at = ? "
                "WHERE kind = 'reservation' AND status = 'reserved' "
                "AND json_extract(data, '$.zone_id') = ? "
                "AND json_extract(data, '$.expires_at') <= ?",
                (now, zone_id, now),
            )
            active_row = connection.execute(
                "SELECT COALESCE(SUM(CAST(json_extract(data, '$.count') AS INTEGER)), 0) AS total "
                "FROM entities WHERE kind = 'reservation' AND status = 'reserved' "
                "AND json_extract(data, '$.zone_id') = ? "
                "AND json_extract(data, '$.expires_at') > ?",
                (zone_id, now),
            ).fetchone()
            active = int(active_row["total"])
            if occupancy + active + int(count) > capacity:
                raise ConflictError("zone capacity would be exceeded by this reservation")
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, 'reservation', 'reserved', 1, ?, ?, ?, ?)",
                (reservation_id, payload, operator_id, now, now),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(reservation_id)

    def confirm_reservation(self, reservation_id):
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, kind, status, data FROM entities WHERE id = ?", (reservation_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("reservation not found: " + reservation_id)
            if row["kind"] != "reservation":
                raise InvalidTransition("entity is not a reservation")
            if row["status"] != "reserved":
                raise InvalidTransition("reservation is not in reserved status")
            data = json.loads(row["data"])
            expires_at = data.get("expires_at")
            if expires_at and str(expires_at) <= now:
                connection.execute(
                    "UPDATE entities SET status = 'expired', updated_at = ? WHERE id = ?",
                    (now, reservation_id),
                )
                connection.commit()
                raise ConflictError("reservation has expired")
            data["confirmed_at"] = now
            new_payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
            connection.execute(
                "UPDATE entities SET status = 'confirmed', version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ?",
                (new_payload, now, reservation_id),
            )
            zone_id = data["zone_id"]
            count = int(data["count"])
            zone_row = connection.execute(
                "SELECT id, status, data FROM entities WHERE id = ?", (zone_id,)
            ).fetchone()
            if not zone_row:
                raise NotFoundError("zone not found: " + zone_id)
            zone_data = json.loads(zone_row["data"])
            zone_data["current_occupancy"] = int(zone_data.get("current_occupancy", 0)) + count
            zone_payload = json.dumps(zone_data, ensure_ascii=False, sort_keys=True)
            connection.execute(
                "UPDATE entities SET data = ?, version = version + 1, updated_at = ? WHERE id = ?",
                (zone_payload, now, zone_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(reservation_id), self.get_entity(zone_id)

    def release_reservation(self, reservation_id, reason):
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, kind, status, data FROM entities WHERE id = ?", (reservation_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("reservation not found: " + reservation_id)
            if row["kind"] != "reservation":
                raise InvalidTransition("entity is not a reservation")
            if row["status"] != "reserved":
                raise InvalidTransition("reservation is not in reserved status")
            data = json.loads(row["data"])
            data["released_at"] = now
            data["release_reason"] = reason
            new_payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
            connection.execute(
                "UPDATE entities SET status = 'released', version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ?",
                (new_payload, now, reservation_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(reservation_id)

    def change_zone_capacity(self, zone_id, new_capacity):
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, status, data FROM entities WHERE id = ?", (zone_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("zone not found: " + zone_id)
            zone_data = json.loads(row["data"])
            zone_data["capacity"] = int(new_capacity)
            zone_payload = json.dumps(zone_data, ensure_ascii=False, sort_keys=True)
            connection.execute(
                "UPDATE entities SET data = ?, version = version + 1, updated_at = ? WHERE id = ?",
                (zone_payload, now, zone_id),
            )
            connection.execute(
                "UPDATE entities SET status = 'released', updated_at = ? "
                "WHERE kind = 'reservation' AND status = 'reserved' "
                "AND json_extract(data, '$.zone_id') = ?",
                (now, zone_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(zone_id)

    def get_zone_detail(self, zone_id):
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (zone_id,)
            ).fetchone()
            if not row:
                connection.rollback()
                return None
            zone = self._entity_from_row(row)
            reserved_row = connection.execute(
                "SELECT COALESCE(SUM(CAST(json_extract(data, '$.count') AS INTEGER)), 0) AS total "
                "FROM entities WHERE kind = 'reservation' AND status = 'reserved' "
                "AND json_extract(data, '$.zone_id') = ? "
                "AND json_extract(data, '$.expires_at') > ?",
                (zone_id, now),
            ).fetchone()
            reserved_count = int(reserved_row["total"])
            capacity = int(zone["data"].get("capacity", 0))
            occupancy = int(zone["data"].get("current_occupancy", 0))
            zone["data"]["actual_occupancy"] = occupancy
            zone["data"]["reserved_count"] = reserved_count
            zone["data"]["remaining_capacity"] = capacity - occupancy - reserved_count
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return zone

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
