"""GraphQL subscriptions over ``graphql-transport-ws``.

Security properties:
* Exact Origin check before the handshake; JWT only in ``connection_init`` (never URL or
  cookie); authentication must complete within 5 s.
* <= 2 connections/user, <= 5 subscriptions/connection, 16 KiB frames, 60 client messages
  per minute, outbound buffer of 100 notifications (or 1 MiB) then disconnect.
* Authorization is checked on subscribe and before every delivery (the principal is
  reloaded from PostgreSQL and the incident refetched). Idle connections are rechecked for
  account/session/grant revocation every 30 s. The socket closes when the JWT expires.
* Frames are never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from fastapi import APIRouter, WebSocket
from graphql import ExecutionResult, OperationType
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.websockets import WebSocketDisconnect, WebSocketState

from pulseops.container import Container, current_request_id
from pulseops.errors import AppError, DependencyUnavailable, RateLimited, Unauthenticated
from pulseops.gql.context import GqlContext
from pulseops.gql.schema import QueryRejected, format_error, prepare, schema
from pulseops.logging import log_event
from pulseops.notifications import CHANNEL, Note, parse_note
from pulseops.policy import Principal, can_read_service
from pulseops.ratelimit import Bucket, make_redis
from pulseops.security.tokens import TokenInvalid
from pulseops.services.auth import AuthService
from pulseops.services.metrics import operation_as_of

router = APIRouter()

SUBPROTOCOL = "graphql-transport-ws"
INIT_TIMEOUT_S = 5.0
MAX_FRAME_BYTES = 16 * 1024
MAX_SUBSCRIPTIONS = 5
MAX_CONNECTIONS_PER_USER = 2
CONTROL_MESSAGES_PER_MIN = 60
OUTBOUND_MAX_EVENTS = 100
OUTBOUND_MAX_BYTES = 1024 * 1024
RECHECK_INTERVAL_S = 30.0
CONN_LEASE_MS = 90_000

# Close codes (4400-4499 per graphql-transport-ws conventions).
CLOSE_BAD_REQUEST = 4400
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_INIT_TIMEOUT = 4408
CLOSE_DUPLICATE_SUBSCRIBER = 4409
CLOSE_TOO_MANY = 4429
CLOSE_TOKEN_EXPIRED = 4498  # client should refresh its access token and reconnect
CLOSE_BACKPRESSURE = 4499  # client should reconnect and refetch


class _Listener:
    def __init__(self, conn: Connection) -> None:
        self.queue: asyncio.Queue[Note] = asyncio.Queue(maxsize=OUTBOUND_MAX_EVENTS)
        self.conn = conn

    def offer(self, note: Note) -> None:
        if self.conn.buffered_bytes + note.size > OUTBOUND_MAX_BYTES:
            self.conn.overflow()
            return
        try:
            self.queue.put_nowait(note)
            self.conn.buffered_bytes += note.size
        except asyncio.QueueFull:
            self.conn.overflow()


class IncidentHub:
    """One Redis subscriber per API process fanning notifications out to local sockets.

    Redis Pub/Sub is lossy: subscribers can miss events (e.g. during a Redis outage).
    Clients therefore refetch incident lists on reconnect and every 30 s.
    """

    def __init__(self, c: Container) -> None:
        self.c = c
        self._listeners: dict[uuid.UUID, set[_Listener]] = {}
        self._task: asyncio.Task[None] | None = None
        self.connected = False

    def register(self, service_id: uuid.UUID, listener: _Listener) -> None:
        self._listeners.setdefault(service_id, set()).add(listener)

    def unregister(self, service_id: uuid.UUID, listener: _Listener) -> None:
        group = self._listeners.get(service_id)
        if group:
            group.discard(listener)
            if not group:
                self._listeners.pop(service_id, None)

    def dispatch(self, note: Note) -> None:
        for listener in list(self._listeners.get(note.service_id, ())):
            listener.offer(note)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="incident-hub")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        s = self.c.settings
        backoff = 0.5
        while True:
            client: Redis | None = None
            try:
                client = make_redis(s.redis_host, s.redis_port, s.redis_user, s.redis_password, use_ssl=s.redis_ssl, timeout=5.0)
                pubsub = client.pubsub(ignore_subscribe_messages=True)
                await pubsub.subscribe(CHANNEL)
                self.connected, backoff = True, 0.5
                log_event("ws.hub_connected")
                while True:
                    msg = await pubsub.get_message(timeout=1.0)
                    if msg and msg.get("type") == "message":
                        note = parse_note(msg["data"])
                        if note is not None:
                            self.dispatch(note)
            except asyncio.CancelledError:
                raise
            except (RedisError, OSError) as exc:
                self.connected = False
                log_event("ws.hub_disconnected", error_class=type(exc).__name__)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)
            finally:
                if client is not None:
                    with contextlib.suppress(Exception):
                        await client.aclose()


class SubscriptionHooks:
    """Exposed to the subscription resolver as ``info.context.ws``."""

    def __init__(self, conn: Connection, op_id: str, ctx_holder: dict[str, GqlContext]) -> None:
        self.conn = conn
        self.op_id = op_id
        self._ctx_holder = ctx_holder
        self.service_id: uuid.UUID | None = None
        self._delivered: dict[uuid.UUID, int] = {}

    @contextlib.asynccontextmanager
    async def listen(self, service_id: uuid.UUID) -> AsyncIterator[asyncio.Queue[Note]]:
        listener = _Listener(self.conn)
        self.service_id = service_id
        hub = self.conn.hub
        hub.register(service_id, listener)
        try:
            yield _AccountingQueue(listener, self.conn)  # type: ignore[misc]
        finally:
            hub.unregister(service_id, listener)
            # Release buffered-byte accounting for notifications that were never consumed.
            while not listener.queue.empty():
                note = listener.queue.get_nowait()
                self.conn.buffered_bytes = max(0, self.conn.buffered_bytes - note.size)

    async def refresh_principal(self) -> None:
        principal = await self.conn.refresh_principal()
        ctx = self._ctx_holder["ctx"]
        ctx.reset(principal, operation_as_of(self.conn.c.now()))

    def should_deliver(self, incident_id: uuid.UUID, version: int) -> bool:
        if self._delivered.get(incident_id, 0) >= version:
            return False
        self._delivered[incident_id] = version
        if len(self._delivered) > 1000:
            self._delivered.clear()
        return True


class _AccountingQueue:
    """Queue facade that releases buffered-byte accounting as notes are consumed."""

    def __init__(self, listener: _Listener, conn: Connection) -> None:
        self._l, self._conn = listener, conn

    async def get(self) -> Note:
        note = await self._l.queue.get()
        self._conn.buffered_bytes = max(0, self._conn.buffered_bytes - note.size)
        return note


class Connection:
    def __init__(self, ws: WebSocket, c: Container, hub: IncidentHub) -> None:
        self.ws, self.c, self.hub = ws, c, hub
        self.conn_id = uuid.uuid4().hex
        self.principal: Principal | None = None
        self.claims_user: uuid.UUID | None = None
        self.claims_session: uuid.UUID | None = None
        self.token_expires_at: datetime | None = None
        self.subs: dict[str, tuple[asyncio.Task[None], SubscriptionHooks]] = {}
        self.buffered_bytes = 0
        self._send_lock = asyncio.Lock()
        self._msg_times: deque[float] = deque()
        self._closed = asyncio.Event()
        self._close_code: int | None = None
        self._lease_local = False

    # ---- lifecycle ----

    async def serve(self) -> None:
        settings = self.c.settings
        origin = self.ws.headers.get("origin")
        offered = [p.strip() for p in self.ws.headers.get("sec-websocket-protocol", "").split(",")]
        if not origin or origin not in settings.allowed_origins or SUBPROTOCOL not in offered:
            await self.ws.close(code=1008)  # rejected before the handshake completes
            return
        await self.ws.accept(subprotocol=SUBPROTOCOL)
        try:
            if not await self._handshake():
                return
            await self._send({"type": "connection_ack"})
            tasks = [
                asyncio.create_task(self._receive_loop(), name="ws-recv"),
                asyncio.create_task(self._recheck_loop(), name="ws-recheck"),
                asyncio.create_task(self._expiry_timer(), name="ws-expiry"),
            ]
            await self._closed.wait()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await self._teardown()

    async def _teardown(self) -> None:
        for task, _ in list(self.subs.values()):
            task.cancel()
        self.subs.clear()
        await self._release_conn_slot()
        if self.ws.application_state != WebSocketState.DISCONNECTED:
            with contextlib.suppress(Exception):
                await self.ws.close(code=self._close_code or 1000)

    def close(self, code: int) -> None:
        if self._close_code is None:
            self._close_code = code
        self._closed.set()

    def overflow(self) -> None:
        log_event("ws.backpressure_disconnect")
        self.close(CLOSE_BACKPRESSURE)

    # ---- authentication ----

    async def _handshake(self) -> bool:
        try:
            raw = await asyncio.wait_for(self.ws.receive_text(), timeout=INIT_TIMEOUT_S)
        except TimeoutError:
            self.close(CLOSE_INIT_TIMEOUT)
            return False
        except (WebSocketDisconnect, RuntimeError, KeyError):
            self.close(1000)
            return False
        msg = self._parse(raw)
        if msg is None or msg.get("type") != "connection_init":
            self.close(CLOSE_BAD_REQUEST if msg is not None else self._close_code or CLOSE_BAD_REQUEST)
            return False
        payload = msg.get("payload") or {}
        auth = payload.get("authorization") if isinstance(payload, dict) else None
        try:
            if not isinstance(auth, str) or not auth.startswith("Bearer "):
                raise Unauthenticated()
            claims = self.c.jwt.validate(auth[7:].strip())
            self.claims_user, self.claims_session, self.token_expires_at = claims.user_id, claims.session_id, claims.expires_at
            self.principal = await AuthService(self.c).load_principal(claims.user_id, claims.session_id)
        except (TokenInvalid, Unauthenticated):
            self.close(CLOSE_FORBIDDEN)
            return False
        except Exception:
            self.close(1011)
            return False
        if not await self._acquire_conn_slot():
            self.close(CLOSE_TOO_MANY)
            return False
        return True

    async def refresh_principal(self) -> Principal:
        """Reload account/session/grants; close the connection if access was revoked."""
        assert self.claims_user and self.claims_session
        try:
            self.principal = await AuthService(self.c).load_principal(self.claims_user, self.claims_session)
        except Unauthenticated:
            self.close(CLOSE_FORBIDDEN)
            raise
        except Exception as exc:
            # Auth-state/DB failure always denies protected access.
            self.close(1011)
            raise DependencyUnavailable() from exc
        return self.principal

    # ---- per-user connection slots ----

    _local_slots: dict[uuid.UUID, int] = {}

    def _slot_key(self) -> str:
        assert self.principal and self.principal.user_id
        return f"cc:ws:user:{self.principal.user_id}"

    async def _acquire_conn_slot(self) -> bool:
        assert self.principal and self.principal.user_id
        try:
            await self.c.limiter.acquire_lease([(self._slot_key(), MAX_CONNECTIONS_PER_USER)], lease_ms=CONN_LEASE_MS, member=self.conn_id)
            return True
        except RateLimited:
            return False
        except DependencyUnavailable:
            # Stricter local fallback while Redis is unavailable (reads only).
            count = Connection._local_slots.get(self.principal.user_id, 0)
            if count >= MAX_CONNECTIONS_PER_USER:
                return False
            Connection._local_slots[self.principal.user_id] = count + 1
            self._lease_local = True
            return True

    async def _lease_refresh(self) -> None:
        with contextlib.suppress(AppError):
            await self.c.limiter.acquire_lease([(self._slot_key(), MAX_CONNECTIONS_PER_USER)], lease_ms=CONN_LEASE_MS, member=self.conn_id)

    async def _release_conn_slot(self) -> None:
        if self.principal is None or self.principal.user_id is None:
            return
        if self._lease_local:
            uid = self.principal.user_id
            Connection._local_slots[uid] = max(0, Connection._local_slots.get(uid, 1) - 1)
            return
        await self.c.limiter.release_lease([self._slot_key()], self.conn_id)

    # ---- loops ----

    async def _expiry_timer(self) -> None:
        assert self.token_expires_at is not None
        delay = (self.token_expires_at - self.c.now()).total_seconds()
        await asyncio.sleep(max(0.0, delay))
        self.close(CLOSE_TOKEN_EXPIRED)

    async def _recheck_loop(self) -> None:
        while True:
            await asyncio.sleep(RECHECK_INTERVAL_S)
            try:
                principal = await self.refresh_principal()
            except AppError:
                return
            if not self._lease_local:
                await self._lease_refresh()
            for op_id, (task, hooks) in list(self.subs.items()):
                if hooks.service_id is not None and not can_read_service(principal, hooks.service_id):
                    task.cancel()
                    self.subs.pop(op_id, None)
                    await self._send(
                        {"id": op_id, "type": "error", "payload": [{"message": "Not found", "extensions": {"code": "NOT_FOUND"}}]}
                    )

    def _rate_ok(self) -> bool:
        now = time.monotonic()
        while self._msg_times and now - self._msg_times[0] > 60:
            self._msg_times.popleft()
        self._msg_times.append(now)
        return len(self._msg_times) <= CONTROL_MESSAGES_PER_MIN

    def _parse(self, raw: str) -> dict[str, Any] | None:
        if len(raw.encode()) > MAX_FRAME_BYTES:
            self.close(1009)
            return None
        try:
            msg = json.loads(raw)
        except ValueError:
            self.close(CLOSE_BAD_REQUEST)
            return None
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
            self.close(CLOSE_BAD_REQUEST)
            return None
        return msg

    async def _receive_loop(self) -> None:
        try:
            while True:
                raw = await self.ws.receive_text()
                if not self._rate_ok():
                    self.close(CLOSE_TOO_MANY)
                    return
                msg = self._parse(raw)
                if msg is None:
                    return
                kind = msg["type"]
                if kind == "ping":
                    await self._send({"type": "pong"})
                elif kind == "pong":
                    continue
                elif kind == "subscribe":
                    await self._subscribe(msg)
                elif kind == "complete":
                    op_id = msg.get("id")
                    entry = self.subs.pop(op_id, None) if isinstance(op_id, str) else None
                    if entry:
                        entry[0].cancel()
                elif kind == "connection_init":
                    self.close(CLOSE_TOO_MANY)  # "Too many initialisation requests"
                    return
                else:
                    self.close(CLOSE_BAD_REQUEST)
                    return
        except (WebSocketDisconnect, RuntimeError, KeyError):
            self.close(1000)

    # ---- subscriptions ----

    async def _send(self, msg: dict[str, Any]) -> None:
        data = json.dumps(msg, separators=(",", ":"), default=str)
        if len(data) > OUTBOUND_MAX_BYTES:
            self.overflow()
            return
        async with self._send_lock:
            if self.ws.application_state == WebSocketState.CONNECTED and not self._closed.is_set():
                with contextlib.suppress(Exception):
                    await self.ws.send_text(data)

    async def _error(self, op_id: str, code: str, message: str = "Subscription rejected") -> None:
        await self._send({"id": op_id, "type": "error", "payload": [{"message": message, "extensions": {"code": code}}]})

    async def _subscribe(self, msg: dict[str, Any]) -> None:
        op_id, payload = msg.get("id"), msg.get("payload")
        if not isinstance(op_id, str) or not op_id or len(op_id) > 64 or not isinstance(payload, dict):
            self.close(CLOSE_BAD_REQUEST)
            return
        if op_id in self.subs:
            self.close(CLOSE_DUPLICATE_SUBSCRIBER)
            return
        if len(self.subs) >= MAX_SUBSCRIPTIONS:
            await self._error(op_id, "TOO_MANY_SUBSCRIPTIONS")
            return
        variables = payload.get("variables") or {}
        if not isinstance(variables, dict):
            await self._error(op_id, "BAD_REQUEST")
            return
        try:
            prepared = prepare(payload.get("query"), payload.get("operationName"), variables)
        except QueryRejected as exc:
            await self._error(op_id, exc.code)
            return
        if prepared.operation_type != OperationType.SUBSCRIPTION:
            await self._error(op_id, "BAD_REQUEST")
            return
        try:
            principal = await self.refresh_principal()
            uid = str(principal.user_id)
            try:
                await self.c.limiter.consume([Bucket(f"rl:api:user:{uid}", 2, 120, 1)])
            except DependencyUnavailable:
                self.c.local_limiter.consume(uid)  # subscribing is a read
        except AppError as exc:
            await self._error(op_id, exc.code)
            return
        holder: dict[str, GqlContext] = {}
        hooks = SubscriptionHooks(self, op_id, holder)
        ctx = GqlContext(c=self.c, principal=principal, as_of=operation_as_of(self.c.now()), request_id=current_request_id(), ws=hooks)
        holder["ctx"] = ctx
        result = await schema.subscribe(
            payload["query"], variable_values=variables, context_value=ctx, operation_name=payload.get("operationName")
        )
        if isinstance(result, ExecutionResult) or not hasattr(result, "__aiter__"):
            errors = getattr(result, "errors", None) or []
            await self._send(
                {
                    "id": op_id,
                    "type": "error",
                    "payload": [format_error(e) for e in errors]
                    or [{"message": "Subscription rejected", "extensions": {"code": "BAD_REQUEST"}}],
                }
            )
            return
        self.subs[op_id] = (asyncio.create_task(self._pump(op_id, result), name=f"ws-sub-{op_id}"), hooks)

    async def _pump(self, op_id: str, stream: Any) -> None:
        try:
            async for item in stream:
                if getattr(item, "errors", None):
                    await self._send({"id": op_id, "type": "error", "payload": [format_error(e) for e in item.errors]})
                    return
                await self._send({"id": op_id, "type": "next", "payload": {"data": item.data}})
            await self._send({"id": op_id, "type": "complete"})
        except asyncio.CancelledError:
            raise
        except AppError as exc:
            await self._error(op_id, exc.code, exc.message)
        except Exception as exc:
            log_event("ws.subscription_failed", error_class=type(exc).__name__)
            await self._error(op_id, "INTERNAL", "Internal error")
        finally:
            self.subs.pop(op_id, None)
            with contextlib.suppress(Exception):
                await stream.aclose()


@router.websocket("/graphql")
async def graphql_ws(ws: WebSocket) -> None:
    c: Container = ws.app.state.container
    await Connection(ws, c, ws.app.state.hub).serve()
