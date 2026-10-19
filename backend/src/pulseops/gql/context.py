"""Per-operation GraphQL context.

DataLoaders are created per operation (and per subscription delivery) so cached results
never cross operations or outlive the authorization state they were loaded under. Each
loader batch and each resolver opens its own AsyncSession; sessions are never shared
across concurrently executing sibling resolvers or kept in subscription state.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from strawberry.dataloader import DataLoader

from pulseops.container import Container
from pulseops.errors import NotFound
from pulseops.models import AlertRule, Route, Service
from pulseops.policy import Principal, can_read_service
from pulseops.repositories import catalog as catalog_repo
from pulseops.services.admin import AdminService
from pulseops.services.alerts import AlertService
from pulseops.services.auth import AuthService
from pulseops.services.catalog import CatalogService
from pulseops.services.metrics import MetricsService, ServiceHealth


class Loaders:
    def __init__(self, ctx: GqlContext) -> None:
        self._ctx = ctx
        self.service: DataLoader[uuid.UUID, Service | None] = DataLoader(load_fn=self._load_services)
        self.rule: DataLoader[uuid.UUID, AlertRule | None] = DataLoader(load_fn=self._load_rules)
        self.route: DataLoader[uuid.UUID, Route | None] = DataLoader(load_fn=self._load_routes)
        self.health: DataLoader[uuid.UUID, ServiceHealth | None] = DataLoader(load_fn=self._load_health)

    async def _load_services(self, ids: list[uuid.UUID]) -> list[Service | None]:
        p = self._ctx.principal
        async with self._ctx.c.db.session() as s:
            found = await catalog_repo.services_by_ids(s, [i for i in ids if can_read_service(p, i)])
        return [found.get(i) for i in ids]

    async def _load_rules(self, ids: list[uuid.UUID]) -> list[AlertRule | None]:
        found = await self._ctx.alerts.rules_by_ids(self._ctx.principal, ids)
        return [found.get(i) for i in ids]

    async def _load_routes(self, ids: list[uuid.UUID]) -> list[Route | None]:
        p = self._ctx.principal
        async with self._ctx.c.db.session() as s:
            found = await catalog_repo.routes_by_ids(s, ids)
        return [r if (r := found.get(i)) is not None and can_read_service(p, r.service_id) else None for i in ids]

    async def _load_health(self, ids: list[uuid.UUID]) -> list[ServiceHealth | None]:
        p = self._ctx.principal
        allowed = [i for i in ids if can_read_service(p, i)]
        found = await self._ctx.metrics.health(allowed, self._ctx.as_of)
        return [found.get(i) for i in ids]


@dataclass
class GqlContext:
    c: Container
    principal: Principal
    as_of: datetime
    request_id: uuid.UUID | None
    ws: Any = None  # WebSocket subscription hooks (pulseops.api.ws.Subscription) when applicable
    _loaders: Loaders | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.auth = AuthService(self.c)
        self.catalog = CatalogService(self.c)
        self.metrics = MetricsService(self.c)
        self.alerts = AlertService(self.c)
        self.admin = AdminService(self.c)

    @property
    def loaders(self) -> Loaders:
        if self._loaders is None:
            self._loaders = Loaders(self)
        return self._loaders

    def reset(self, principal: Principal, as_of: datetime) -> None:
        """Fresh authorization state and loaders (used before each subscription delivery)."""
        self.principal, self.as_of, self._loaders = principal, as_of, None


def parse_id(value: str) -> uuid.UUID:
    """Malformed IDs are indistinguishable from absent/inaccessible objects."""
    try:
        if len(value) > 64:
            raise ValueError
        return uuid.UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise NotFound() from exc
