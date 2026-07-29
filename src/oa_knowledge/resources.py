"""Persistent cross-process resource leases for OCR and LLM workers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from oa_knowledge.db.models import ResourceLease


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ResourceCoordinator:
    def __init__(self, engine) -> None:
        self.engine = engine

    def acquire(self, resource_kind: str, owner_id: str, *, ttl_seconds: int, uses_local_gpu: bool) -> int | None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        now = _utcnow()
        resource_key = "local_gpu" if uses_local_gpu else resource_kind
        with Session(self.engine) as session:
            session.execute(
                delete(ResourceLease).where(
                    ResourceLease.resource_key == resource_key,
                    ResourceLease.expires_at <= now,
                )
            )
            lease = ResourceLease(
                resource_key=resource_key,
                resource_kind=resource_kind,
                owner_id=owner_id,
                acquired_at=now,
                heartbeat_at=now,
                expires_at=now + timedelta(seconds=ttl_seconds),
            )
            session.add(lease)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return None
            return lease.id

    def heartbeat(self, lease_id: int, owner_id: str, *, ttl_seconds: int) -> bool:
        now = _utcnow()
        with Session(self.engine) as session:
            result = session.execute(
                update(ResourceLease)
                .where(ResourceLease.id == lease_id, ResourceLease.owner_id == owner_id)
                .values(heartbeat_at=now, expires_at=now + timedelta(seconds=ttl_seconds))
            )
            session.commit()
            return result.rowcount == 1

    def release(self, lease_id: int, owner_id: str) -> bool:
        with Session(self.engine) as session:
            result = session.execute(
                delete(ResourceLease).where(ResourceLease.id == lease_id, ResourceLease.owner_id == owner_id)
            )
            session.commit()
            return result.rowcount == 1

    def expire_for_test(self, lease_id: int, expires_at: datetime) -> None:
        value = expires_at.astimezone(timezone.utc).replace(tzinfo=None) if expires_at.tzinfo else expires_at
        with Session(self.engine) as session:
            session.execute(update(ResourceLease).where(ResourceLease.id == lease_id).values(expires_at=value))
            session.commit()

    def recover_dead_owners(self, owner_alive) -> int:
        """Remove only leases tied to worker PIDs that no longer exist."""
        with Session(self.engine) as session:
            rows = session.scalars(select(ResourceLease)).all()
            dead = [row.id for row in rows if row.owner_id.startswith("worker-") and not owner_alive(row.owner_id.split(":", 1)[0])]
            if dead:
                session.execute(delete(ResourceLease).where(ResourceLease.id.in_(dead)))
            session.commit()
            return len(dead)
