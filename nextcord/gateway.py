# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
import logging
import random
import struct
import sys
import threading
import time
import zlib
from collections import namedtuple
from typing import TYPE_CHECKING, Awaitable, Callable, Dict, List, Optional, Union, cast

import aiohttp

from . import utils
from .activity import BaseActivity
from .enums import SpeakingState
from .errors import ConnectionClosed, InvalidArgument

if TYPE_CHECKING:
    from typing import Any, Protocol

    from .client import Client
    from .http import HTTPClient
    from .state import ConnectionState
    from .types.activity import Activity
    from .types.checks import DispatchProtocol
    from .types.gateway import ReceivableGatewayEvent
    from .voice_client import VoiceClient

    class VariadicArgNone(Protocol):
        def __call__(self, *args: Any) -> None:
            ...


_log = logging.getLogger(__name__)


# TODO: where to put?
DISPATCH = 0
HEARTBEAT = 1
PRESENCE = 2
IDENTIFY = 2
RECONNECT = 7
REQUEST_MEMBERS = 8
INVALID_SESSION = 9
HELLO = 10
HEARTBEAT_ACK = 11

__all__ = (
    "DiscordWebSocket",
    "DiscordVoiceWebSocket",
    "ReconnectWebSocket",
)


class ReconnectWebSocket(Exception):
    """Signals to safely reconnect the websocket."""

    def __init__(self, shard_id: Optional[int], *, resume: bool = True) -> None:
        self.shard_id = shard_id
        self.resume = resume
        self.op = "RESUME" if resume else "IDENTIFY"


class WebSocketClosure(Exception):
    """An exception to make up for the fact that aiohttp doesn't signal closure."""


EventListener = namedtuple("EventListener", "predicate event result future")  # type: ignore


class GatewayRatelimiter:
    def __init__(self, count: int = 110, per: float = 60.0) -> None:
        # The default is 110 to give room for at least 10 heartbeats per minute
        self.max = count
        self.remaining = count
        self.window = 0.0
        self.per = per
        self.lock = asyncio.Lock()
        self.shard_id: Optional[int] = None

    def is_ratelimited(self) -> bool:
        current = time.time()
        if current > self.window + self.per:
            return False
        return self.remaining == 0

    def get_delay(self) -> float:
        current = time.time()

        if current > self.window + self.per:
            self.remaining = self.max

        if self.remaining == self.max:
            self.window = current

        if self.remaining == 0:
            return self.per - (current - self.window)

        self.remaining -= 1
        if self.remaining == 0:
            self.window = current

        return 0.0

    async def block(self) -> None:
        async with self.lock:
            delta = self.get_delay()
            if delta:
                _log.warning(
                    "WebSocket in shard ID %s is ratelimited, waiting %.2f seconds",
                    self.shard_id,
                    delta,
                )
                await asyncio.sleep(delta)


class DiscordClientWebSocketResponse(aiohttp.ClientWebSocketResponse):
    async def close(self, *, code: int = 4000, message: bytes = b"") -> bool:
        return await super().close(code=code, message=message)


class Shard:
    """Implements a sharded connection to Discord's gateway.

    A shard can work independently, similarly to the old ``DiscordWebSocket`` class,
    or can work in a cluster together.

    Attributes
    ----------
    """

    __slots__ = (
        "_socket",
        "_http",
        "_token",
        "_dispatch",
        "dispatch_listeners",
        "discord_parsers",

        "_connection_task",
        "_heartbeat_task",
        "_heartbeat_ack_received",
        "heartbeat_interval",
        "_heartbeat_sent_time",
        "latency",
        "intents",
        "session_id",
        "resume_url",
        "sequence",
        "shard_id",
        "shard_count",
        "debug",
        "activity",
        "status",

        "_zlib",
        "_buffer",
        "_rate_limiter",
    )

    def __init__(
        self,
        http: HTTPClient,
        token: str,
        dispatch: DispatchProtocol,
        *,
        intents: int,
        shard_id: int = 0,
        shard_count: int = 1,
        debug: bool = False,
    ):
        self._socket: Optional[aiohttp.ClientWebSocketResponse] = None
        self._http: HTTPClient = http
        self._token: str = token
        self._dispatch: DispatchProtocol = dispatch
        self.dispatch_listeners: List[EventListener] = []
        self.discord_parsers: Dict[str, Any] = {}

        self._connection_task: Optional[asyncio.Task[None]] = None
        self._heartbeat_task: Optional[asyncio.Task[None]] = None

        self._heartbeat_ack_received: Optional[asyncio.Future[None]] = None
        self.heartbeat_interval: float = 0.0
        self._heartbeat_sent_time: float = 0.0
        self.latency: float = 0.0

        self.intents: int = intents
        self.session_id: Optional[str] = None
        self.resume_url: Optional[str] = None
        self.sequence: Optional[int] = None
        self.shard_id: int = shard_id
        self.shard_count: int = shard_count
        self.debug: bool = debug
        self.activity: Optional[Activity] = None
        self.status: Optional[str] = None

        self._zlib = zlib.decompressobj()
        self._buffer: bytearray = bytearray()
        self._rate_limiter: GatewayRatelimiter = GatewayRatelimiter()

    async def send(self, data: str, /) -> None:
        if self._socket is None:
            raise AttributeError("WebSocket has not been opened.")

        await self._rate_limiter.block()

        if self.debug:
            self._dispatch("socket_raw_send", data)

        await self._socket.send_str(data)

    def _process_raw_data(self, msg: aiohttp.WSMessage) -> str:
        data = msg.data

        if msg.type is aiohttp.WSMsgType.BINARY:
            data = cast(bytes, data)
            self._buffer.extend(data)

            if len(data) < 4 or data[-4:] != b"\x00\x00\xff\xff":
                # TODO: add more specifics to this error message? maybe the data that was received?
                raise ValueError("Invalid zlib encoded message received from the Gateway!")

            data = self._zlib.decompress(self._buffer).decode("utf-8")
            self._buffer = bytearray()
        else:
            data = cast(str, data)

        return data

    async def connect(self):
        if self.session_id is not None and self.resume_url is not None:
            self._socket = await self._http.ws_connect(self.resume_url)
        else:
            url = await self._http.get_gateway()
            self._socket = await self._http.ws_connect(url)

        self._connection_task = asyncio.create_task(self._connection_loop())
        _log.info("Shard ID %s has successfully established a connection to the gateway.", self.shard_id)

    async def disconnect(self, *, keep_session: bool = False):
        if self._socket is None:
            return

        if self._connection_task:
            self._connection_task.cancel()
            self._connection_task = None

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

        if self._heartbeat_ack_received:
            self._heartbeat_ack_received.cancel()
            self._heartbeat_ack_received = None

        if keep_session:
            await self._socket.close(code=999)
        else:
            await self._socket.close()

            self.resume_url = None
            self.session_id = None
            self.sequence = None

        self._socket = None

    async def _connection_loop(self) -> None:
        if self._socket is None:
            raise AttributeError("WebSocket has not been opened.")

        async for msg in self._socket:
            if msg.type is aiohttp.WSMsgType.TEXT or msg.type is aiohttp.WSMsgType.BINARY:
                data = self._process_raw_data(msg)
                payload = utils.from_json(data)
                await self._process_payload(payload)
            elif msg.type is aiohttp.WSMsgType.ERROR:
                _log.debug("Received error %s", msg)
                await self.disconnect(keep_session=False)
                # TODO: raise exception?
            elif msg.type in (
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSE,
            ):
                _log.debug("Received close %s", msg)
                await self.disconnect(keep_session=False)
                # TODO: raise exception?

    async def _process_payload(self, payload: ReceivableGatewayEvent) -> None:
        seq = payload.get("s")
        if seq is not None:
            self.sequence = seq

        if payload["op"] is DISPATCH:
            event: str = payload["t"]
            data = payload["d"]
            await self.dispatch_events(data, event)

        if payload["op"] is RECONNECT:
            _log.debug("Shard ID %s received RECONNECT opcode.", self.shard_id)
            await self.disconnect()

        if payload["op"] is HEARTBEAT_ACK and self._heartbeat_ack_received:
            self._heartbeat_ack_received.set_result(None)
            ack_time = time.perf_counter()
            self.latency = ack_time - self._heartbeat_sent_time

        if payload["op"] is HEARTBEAT:
            await self.heartbeat()

        if payload["op"] is HELLO:
            self.heartbeat_interval = float(payload["d"]["heartbeat_interval"]) / 1000

            await self.heartbeat()
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

            if self.session_id is not None:
                await self.resume()
            else:
                await self.identify()

        if payload["op"] is INVALID_SESSION:
            await self.disconnect(keep_session=bool(payload["d"]))

    async def dispatch_events(self, data: Any, event: str) -> None:
        if event == "READY":
            self.session_id = data["session_id"]
            self.resume_url = data["resume_gateway_url"]
            # pass back shard ID to ready handler
            data["__shard_id__"] = self.shard_id
            _log.info(
                "Shard ID %s has connected to Gateway: (Session ID: %s). Resume URL specified as %s",
                self.shard_id,
                self.session_id,
                self.resume_url,
            )

        elif event == "RESUMED":
            # pass back the shard ID to the resumed handler
            data["__shard_id__"] = self.shard_id
            _log.info(
                "Shard ID %s has successfully RESUMED session %s.",
                self.shard_id,
                self.session_id,
            )

        # TODO: to prevent the weird hack to incorporate the shard id into the data,
        # I think we should add a new parameter for discord parsers that is the shard that
        # received the request.

        try:
            func = self.discord_parsers[event]
        except KeyError:
            _log.debug("Unknown event %s.", event)
        else:
            await utils.maybe_coroutine(func, data)

        # remove the dispatched listeners
        removed = []
        for index, entry in enumerate(self.dispatch_listeners):
            if entry.event != event:
                continue

            future = entry.future
            if future.cancelled():
                removed.append(index)
                continue

            try:
                valid = entry.predicate(data)
            except Exception as exc:
                future.set_exception(exc)
                removed.append(index)
            else:
                if valid:
                    ret = data if entry.result is None else entry.result(data)
                    future.set_result(ret)
                    removed.append(index)

        for index in reversed(removed):
            del self.dispatch_listeners[index]

    async def _heartbeat_loop(self) -> None:
        if self._socket is None:
            raise AttributeError("WebSocket has not been opened.")

        loop = asyncio.get_running_loop()
        timeout = self.heartbeat_interval * random.random()
        await asyncio.sleep(timeout)

        while not self._socket.closed:
            await self.heartbeat()

            self._heartbeat_ack_received = loop.create_future()
            await self._heartbeat_ack_received

            await asyncio.sleep(self.heartbeat_interval)

    async def heartbeat(self):
        """Sends the HEARTBEAT packet."""
        payload = {"op": 1, "d": self.sequence}
        await self.send(utils.to_json(payload))

        self._heartbeat_sent_time = time.perf_counter()

    async def identify(self) -> None:
        """Sends the IDENTIFY packet."""
        payload = {
            "op": 2,
            "d": {
                "token": self._token,
                "properties": {
                    "os": sys.platform,
                    "browser": "nextcord",
                    "device": "nextcord",
                },
                "compress": True,
                "large_threshold": 250,
                "intents": self.intents,
                "shard": [self.shard_id, self.shard_count],
            },
        }

        if self.activity is not None or self.status is not None:
            payload["d"]["presence"] = {
                "status": self.status,
                "game": self.activity,
                "since": 0,
                "afk": False,
            }

        #await self.call_hooks("before_identify", self.shard_id, initial=self._initial_identify)
        await self.send(utils.to_json(payload))
        _log.info("Shard ID %s has sent the IDENTIFY payload.", self.shard_id)

    async def resume(self) -> None:
        """Sends the RESUME packet."""
        payload = {
            "op": 6,
            "d": {
                "seq": self.sequence,
                "session_id": self.session_id,
                "token": self._token,
            },
        }

        await self.send(utils.to_json(payload))
        _log.info("Shard ID %s has sent the RESUME payload.", self.shard_id)

    async def change_presence(
        self,
        *,
        activity: Optional[BaseActivity] = None,
        status: Optional[str] = None,
        since: float = 0.0,
    ) -> None:
        if activity is not None:
            activities: List[Activity] = [activity.to_dict()]
        else:
            activities: List[Activity] = []

        if status == "idle":
            since = int(time.time() * 1000)

        payload = {
            "op": PRESENCE,
            "d": {"activities": activities, "afk": False, "since": since, "status": status},
        }

        sent = utils.to_json(payload)
        await self.send(sent)
        _log.debug("Shard ID %s has sent %s to change status.", self.shard_id, sent)

    async def request_chunks(
        self,
        guild_id: int,
        query: Optional[str] = None,
        *,
        limit: int,
        user_ids: Optional[List[int]] = None,
        presences: bool = False,
        nonce: Optional[str] = None,
    ) -> None:
        payload = {
            "op": REQUEST_MEMBERS,
            "d": {"guild_id": guild_id, "presences": presences, "limit": limit},
        }

        if nonce:
            payload["d"]["nonce"] = nonce

        if user_ids:
            payload["d"]["user_ids"] = user_ids

        if query is not None:
            payload["d"]["query"] = query

        await self.send(utils.to_json(payload))

class DiscordWebSocket:
    """Implements a WebSocket for Discord's gateway.

    Attributes
    ----------
    DISPATCH
        Receive only. Denotes an event to be sent to Discord, such as READY.
    HEARTBEAT
        When received tells Discord to keep the connection alive.
        When sent asks if your connection is currently alive.
    IDENTIFY
        Send only. Starts a new session.
    PRESENCE
        Send only. Updates your presence.
    VOICE_STATE
        Send only. Starts a new connection to a voice guild.
    VOICE_PING
        Send only. Checks ping time to a voice guild, do not use.
    RESUME
        Send only. Resumes an existing connection.
    RECONNECT
        Receive only. Tells the client to reconnect to a new gateway.
    REQUEST_MEMBERS
        Send only. Asks for the full member list of a guild.
    INVALIDATE_SESSION
        Receive only. Tells the client to optionally invalidate the session
        and IDENTIFY again.
    HELLO
        Receive only. Tells the client the heartbeat interval.
    HEARTBEAT_ACK
        Receive only. Confirms receiving of a heartbeat. Not having it implies
        a connection issue.
    GUILD_SYNC
        Send only. Requests a guild sync.
    gateway
        The gateway we are currently connected to.
    token
        The authentication token for discord.
    """

    DISPATCH = 0
    HEARTBEAT = 1
    IDENTIFY = 2
    PRESENCE = 3
    VOICE_STATE = 4
    VOICE_PING = 5
    RESUME = 6
    RECONNECT = 7
    REQUEST_MEMBERS = 8
    INVALIDATE_SESSION = 9
    HELLO = 10
    HEARTBEAT_ACK = 11
    GUILD_SYNC = 12

    if TYPE_CHECKING:
        # AAA 'dynamic attributes', come on
        token: str
        _connection: ConnectionState
        _discord_parsers: dict[Any, Any]
        gateway: Any
        call_hooks: Any
        _initial_identify: bool
        shard_id: int | None
        shard_count: int | None
        _max_heartbeat_timeout: float

    def __init__(
        self, socket: aiohttp.ClientWebSocketResponse, *, loop: asyncio.AbstractEventLoop
    ) -> None:
        self.socket: aiohttp.ClientWebSocketResponse = socket
        self.loop: asyncio.AbstractEventLoop = loop

        # an empty dispatcher to prevent crashes
        self._dispatch: VariadicArgNone = lambda *_args: None
        # generic event listeners
        self._dispatch_listeners: List[EventListener] = []

        # the heartbeat handler
        self._heartbeat_task: Optional[asyncio.Task[None]] = None
        self._heartbeat_ack_received: Optional[asyncio.Future[None]] = None
        self.heartbeat_interval: float = 0.0
        self._heartbeat_sent_time: float = 0.0
        self.latency: float = 0.0

        # ws related stuff
        self.session_id: Optional[str] = None
        self.resume_url: Optional[str] = None
        self.sequence: Optional[int] = None
        self._zlib = zlib.decompressobj()
        self._buffer: bytearray = bytearray()
        self._close_code: Optional[int] = None
        self._rate_limiter: GatewayRatelimiter = GatewayRatelimiter()

    @property
    def open(self) -> bool:
        return not self.socket.closed

    def is_ratelimited(self) -> bool:
        return self._rate_limiter.is_ratelimited()

    def debug_log_receive(self, data: Any, /) -> None:
        self._dispatch("socket_raw_receive", data)

    def log_receive(self, _, /) -> None:
        pass

    @classmethod
    async def from_client(
        cls,
        client: Client,
        *,
        initial: bool = False,
        gateway: Optional[str] = None,
        shard_id: Optional[int] = None,
        session: Optional[str] = None,
        sequence: Optional[int] = None,
        resume: bool = False,
        format_gateway: bool = False,
    ):
        """Creates a main websocket for Discord from a :class:`Client`.

        This is for internal use only.
        """
        if not gateway:
            gateway = await client.http.get_gateway()
        elif format_gateway:
            gateway = client.http.format_websocket_url(gateway)

        socket = await client.http.ws_connect(gateway)
        ws = cls(socket, loop=client.loop)

        # dynamically add attributes needed
        ws.token = client._token  # type: ignore
        ws._connection = client._connection
        ws._discord_parsers = client._connection.parsers
        ws._dispatch = client.dispatch
        ws.gateway = gateway
        ws.call_hooks = client._connection.call_hooks
        ws._initial_identify = initial
        ws.shard_id = shard_id
        ws._rate_limiter.shard_id = shard_id
        ws.shard_count = client._connection.shard_count
        ws.session_id = session
        ws.sequence = sequence
        ws._max_heartbeat_timeout = client._connection.heartbeat_timeout

        if client._enable_debug_events:
            ws.send = ws.debug_send
            ws.log_receive = ws.debug_log_receive

        client._connection._update_references(ws)

        _log.debug("Created websocket connected to %s", gateway)

        # poll event for OP Hello
        await ws.poll_event()

        if not resume:
            await ws.identify()
            return ws

        await ws.resume()
        return ws

    def wait_for(
        self, event: str, predicate: Callable, result: Optional[Callable[[Any], Any]] = None
    ) -> asyncio.Future:
        """Waits for a DISPATCH'd event that meets the predicate.

        Parameters
        ----------
        event: :class:`str`
            The event name in all upper case to wait for.
        predicate
            A function that takes a data parameter to check for event
            properties. The data parameter is the 'd' key in the JSON message.
        result
            A function that takes the same data parameter and executes to send
            the result to the future. If ``None``, returns the data.

        Returns
        -------
        asyncio.Future
            A future to wait for.
        """

        future = self.loop.create_future()
        entry = EventListener(event=event, predicate=predicate, result=result, future=future)
        self._dispatch_listeners.append(entry)
        return future

    async def heartbeat(self) -> None:
        """Sends the HEARTBEAT packet."""
        payload = {"op": self.HEARTBEAT, "d": self.sequence}
        await self.send_heartbeat(payload)

        self._heartbeat_sent_time = time.perf_counter()

    async def identify(self) -> None:
        """Sends the IDENTIFY packet."""
        state = self._connection

        payload = {
            "op": self.IDENTIFY,
            "d": {
                "token": self.token,
                "properties": {
                    "os": sys.platform,
                    "browser": "nextcord",
                    "device": "nextcord",
                },
                "compress": True,
                "large_threshold": 250,
                "intents": state._intents.value,
            },
        }

        if self.shard_id is not None and self.shard_count is not None:
            payload["d"]["shard"] = [self.shard_id, self.shard_count]

        if state._activity is not None or state._status is not None:
            payload["d"]["presence"] = {
                "status": state._status,
                "game": state._activity,
                "since": 0,
                "afk": False,
            }

        await self.call_hooks("before_identify", self.shard_id, initial=self._initial_identify)
        await self.send_as_json(payload)
        _log.info("Shard ID %s has sent the IDENTIFY payload.", self.shard_id)

    async def resume(self) -> None:
        """Sends the RESUME packet."""
        payload = {
            "op": self.RESUME,
            "d": {"seq": self.sequence, "session_id": self.session_id, "token": self.token},
        }

        await self.send_as_json(payload)
        _log.info("Shard ID %s has sent the RESUME payload.", self.shard_id)

    async def _heartbeat_loop(self) -> None:
        timeout = self.heartbeat_interval * random.random()
        await asyncio.sleep(timeout)

        while not self.socket.closed:
            await self.heartbeat()

            self._heartbeat_ack_received = self.loop.create_future()
            await self._heartbeat_ack_received

            await asyncio.sleep(self.heartbeat_interval)

    async def received_message(self, msg: Union[str, bytes], /) -> None:
        if isinstance(msg, bytes):
            self._buffer.extend(msg)

            if len(msg) < 4 or msg[-4:] != b"\x00\x00\xff\xff":
                return
            msg = self._zlib.decompress(self._buffer)
            msg = msg.decode("utf-8")
            self._buffer = bytearray()

        self.log_receive(msg)
        message: Dict[str, Any] = utils.from_json(msg)

        _log.debug("For Shard ID %s: WebSocket Event: %s", self.shard_id, msg)
        event = message.get("t")
        if event:
            self._dispatch("socket_event_type", event)

        op: int = message["op"]
        data: Dict[str, Any] = message["d"]
        seq: Optional[int] = message["s"]
        if seq is not None:
            self.sequence = seq

        if op != self.DISPATCH:
            if op == self.RECONNECT:
                # "reconnect" can only be handled by the Client
                # so we terminate our connection and raise an
                # internal exception signalling to reconnect.
                _log.debug("Received RECONNECT opcode.")
                await self.close()
                raise ReconnectWebSocket(self.shard_id)

            if op == self.HEARTBEAT_ACK and self._heartbeat_ack_received:
                self._heartbeat_ack_received.set_result(None)
                ack_time = time.perf_counter()
                self.latency = ack_time - self._heartbeat_sent_time
                return

            if op == self.HEARTBEAT:
                await self.heartbeat()
                return

            if op == self.HELLO:
                self.heartbeat_interval = data["heartbeat_interval"] / 1000.0

                # send a heartbeat immediately
                await self.heartbeat()
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

                return

            if op == self.INVALIDATE_SESSION:
                if data is True:
                    await self.close()
                    raise ReconnectWebSocket(self.shard_id)

                self.sequence = None
                self.session_id = None
                _log.info("Shard ID %s session has been invalidated.", self.shard_id)
                await self.close(code=1000)
                raise ReconnectWebSocket(self.shard_id, resume=False)

            _log.warning("Unknown OP code %s.", op)
            return

        if event == "READY":
            self._trace = trace = data.get("_trace", [])
            self.sequence: Optional[int] = message["s"]
            self.session_id: Optional[str] = data["session_id"]
            self.resume_url: Optional[str] = data["resume_gateway_url"]
            # pass back shard ID to ready handler
            data["__shard_id__"] = self.shard_id
            _log.info(
                "Shard ID %s has connected to Gateway: %s (Session ID: %s). Resume URL specified as %s",
                self.shard_id,
                ", ".join(trace),
                self.session_id,
                self.resume_url,
            )

        elif event == "RESUMED":
            self._trace = trace = data.get("_trace", [])
            # pass back the shard ID to the resumed handler
            data["__shard_id__"] = self.shard_id
            _log.info(
                "Shard ID %s has successfully RESUMED session %s under trace %s.",
                self.shard_id,
                self.session_id,
                ", ".join(trace),
            )

        try:
            func = self._discord_parsers[event]
        except KeyError:
            _log.debug("Unknown event %s.", event)
        else:
            func(data)

        # remove the dispatched listeners
        removed = []
        for index, entry in enumerate(self._dispatch_listeners):
            if entry.event != event:
                continue

            future = entry.future
            if future.cancelled():
                removed.append(index)
                continue

            try:
                valid = entry.predicate(data)
            except Exception as exc:
                future.set_exception(exc)
                removed.append(index)
            else:
                if valid:
                    ret = data if entry.result is None else entry.result(data)
                    future.set_result(ret)
                    removed.append(index)

        for index in reversed(removed):
            del self._dispatch_listeners[index]

    def _can_handle_close(self) -> bool:
        code = self._close_code or self.socket.close_code
        return code not in (1000, 4004, 4010, 4011, 4012, 4013, 4014)

    async def poll_event(self) -> None:
        """Polls for a DISPATCH event and handles the general gateway loop.

        Raises
        ------
        ConnectionClosed
            The websocket connection was terminated for unhandled reasons.
        """
        try:
            msg = await self.socket.receive(timeout=self._max_heartbeat_timeout)
            if msg.type is aiohttp.WSMsgType.TEXT or msg.type is aiohttp.WSMsgType.BINARY:
                await self.received_message(msg.data)
            elif msg.type is aiohttp.WSMsgType.ERROR:
                _log.debug("Received error %s", msg)
                raise WebSocketClosure
            elif msg.type in (
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSE,
            ):
                _log.debug("Received %s", msg)
                raise WebSocketClosure
        except (asyncio.TimeoutError, WebSocketClosure) as e:
            # Ensure the heartbeat handler is closed
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
                self._heartbeat_task = None

            if isinstance(e, asyncio.TimeoutError):
                _log.info("Timed out receiving packet. Attempting a reconnect.")
                raise ReconnectWebSocket(self.shard_id) from None

            code = self._close_code or self.socket.close_code
            if self._can_handle_close():
                _log.info("Websocket closed with %s, attempting a reconnect.", code)
                raise ReconnectWebSocket(self.shard_id) from None

            _log.info("Websocket closed with %s, cannot reconnect.", code)
            raise ConnectionClosed(self.socket, shard_id=self.shard_id, code=code) from None

    async def debug_send(self, data: Any, /) -> None:
        await self._rate_limiter.block()
        self._dispatch("socket_raw_send", data)
        await self.socket.send_str(data)

    async def send(self, data: Any, /) -> None:
        await self._rate_limiter.block()
        await self.socket.send_str(data)

    async def send_as_json(self, data: Any) -> None:
        try:
            await self.send(utils.to_json(data))
        except RuntimeError as exc:
            if not self._can_handle_close():
                raise ConnectionClosed(self.socket, shard_id=self.shard_id) from exc

    async def send_heartbeat(self, data: Any) -> None:
        # This bypasses the rate limit handling code since it has a higher priority
        try:
            await self.socket.send_str(utils.to_json(data))
        except RuntimeError as exc:
            if not self._can_handle_close():
                raise ConnectionClosed(self.socket, shard_id=self.shard_id) from exc

    async def change_presence(
        self,
        *,
        activity: Optional[BaseActivity] = None,
        status: Optional[str] = None,
        since: float = 0.0,
    ) -> None:
        if activity is not None:
            if not isinstance(activity, BaseActivity):
                raise InvalidArgument("activity must derive from BaseActivity.")
            activities: List[Activity] = [activity.to_dict()]
        else:
            activities: List[Activity] = []

        if status == "idle":
            since = int(time.time() * 1000)

        payload = {
            "op": self.PRESENCE,
            "d": {"activities": activities, "afk": False, "since": since, "status": status},
        }

        sent = utils.to_json(payload)
        _log.debug('Sending "%s" to change status', sent)
        await self.send(sent)

    async def request_chunks(
        self,
        guild_id: int,
        query: Optional[str] = None,
        *,
        limit: int,
        user_ids: Optional[List[int]] = None,
        presences: bool = False,
        nonce: Optional[str] = None,
    ) -> None:
        payload = {
            "op": self.REQUEST_MEMBERS,
            "d": {"guild_id": guild_id, "presences": presences, "limit": limit},
        }

        if nonce:
            payload["d"]["nonce"] = nonce

        if user_ids:
            payload["d"]["user_ids"] = user_ids

        if query is not None:
            payload["d"]["query"] = query

        await self.send_as_json(payload)

    async def voice_state(
        self,
        guild_id: int,
        channel_id: Optional[int],
        self_mute: bool = False,
        self_deaf: bool = False,
    ) -> None:
        payload = {
            "op": self.VOICE_STATE,
            "d": {
                "guild_id": guild_id,
                "channel_id": channel_id,
                "self_mute": self_mute,
                "self_deaf": self_deaf,
            },
        }

        _log.debug("Updating our voice state to %s.", payload)
        await self.send_as_json(payload)

    async def close(self, code: int = 4000) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

        self._close_code = code
        await self.socket.close(code=code)


class DiscordVoiceWebSocket:
    """Implements the websocket protocol for handling voice connections.

    Attributes
    ----------
    IDENTIFY
        Send only. Starts a new voice session.
    SELECT_PROTOCOL
        Send only. Tells discord what encryption mode and how to connect for voice.
    READY
        Receive only. Tells the websocket that the initial connection has completed.
    HEARTBEAT
        Send only. Keeps your websocket connection alive.
    SESSION_DESCRIPTION
        Receive only. Gives you the secret key required for voice.
    SPEAKING
        Send only. Notifies the client if you are currently speaking.
    HEARTBEAT_ACK
        Receive only. Tells you your heartbeat has been acknowledged.
    RESUME
        Sent only. Tells the client to resume its session.
    HELLO
        Receive only. Tells you that your websocket connection was acknowledged.
    RESUMED
        Sent only. Tells you that your RESUME request has succeeded.
    CLIENT_CONNECT
        Indicates a user has connected to voice.
    CLIENT_DISCONNECT
        Receive only.  Indicates a user has disconnected from voice.
    """

    IDENTIFY = 0
    SELECT_PROTOCOL = 1
    READY = 2
    HEARTBEAT = 3
    SESSION_DESCRIPTION = 4
    SPEAKING = 5
    HEARTBEAT_ACK = 6
    RESUME = 7
    HELLO = 8
    RESUMED = 9
    CLIENT_CONNECT = 12
    CLIENT_DISCONNECT = 13

    if TYPE_CHECKING:
        _connection: VoiceClient
        gateway: str
        _max_heartbeat_timeout: float
        thread_id: int

    def __init__(
        self,
        socket: DiscordClientWebSocketResponse,
        loop: asyncio.AbstractEventLoop,
        *,
        hook: Optional[Callable[..., Awaitable[None]]] = None,
    ) -> None:
        self.ws: DiscordClientWebSocketResponse = socket
        self.loop: asyncio.AbstractEventLoop = loop

        # the heartbeat handler
        self._heartbeat_task: Optional[asyncio.Task[None]] = None
        self._heartbeat_ack_received: Optional[asyncio.Future[None]] = None
        self.heartbeat_interval: float = 0.0
        self._heartbeat_sent_time: float = 0.0
        self._latencies: List[float] = []

        self._close_code: Optional[int] = None
        self.secret_key: Optional[List[int]] = None
        self._hook: Optional[Callable[..., Awaitable[None]]] = (
            hook or getattr(self, "_hook", None) or self._default_hook
        )

    async def _default_hook(self, *args: Any) -> None:
        ...

    async def send_as_json(self, data: Any) -> None:
        _log.debug("Sending voice websocket frame: %s.", data)
        await self.ws.send_str(utils.to_json(data))

    send_heartbeat = send_as_json

    async def heartbeat(self) -> None:
        payload = {"op": self.HEARTBEAT, "d": int(time.time() * 1000)}
        await self.send_as_json(payload)

        self._heartbeat_sent_time = time.perf_counter()

    async def resume(self) -> None:
        state = self._connection
        payload = {
            "op": self.RESUME,
            "d": {
                "token": state.token,
                "server_id": str(state.server_id),
                "session_id": state.session_id,
            },
        }
        await self.send_as_json(payload)

    async def identify(self) -> None:
        state = self._connection
        payload = {
            "op": self.IDENTIFY,
            "d": {
                "server_id": str(state.server_id),
                "user_id": str(state.user.id),
                "session_id": state.session_id,
                "token": state.token,
            },
        }
        await self.send_as_json(payload)

    @classmethod
    async def from_client(
        cls,
        client: VoiceClient,
        *,
        resume: bool = False,
        hook: Optional[Callable[..., Awaitable[None]]] = None,
    ):
        """Creates a voice websocket for the :class:`VoiceClient`."""
        gateway = "wss://" + client.endpoint + "/?v=4"
        http = client._state.http
        socket = await http.ws_connect(gateway, compress=15)
        ws = cls(socket, loop=client.loop, hook=hook)
        ws.gateway = gateway
        ws._connection = client
        ws._max_heartbeat_timeout = 60.0
        ws.thread_id = threading.get_ident()

        if resume:
            await ws.resume()
        else:
            await ws.identify()

        return ws

    async def select_protocol(self, ip: str, port: int, mode: str) -> None:
        payload = {
            "op": self.SELECT_PROTOCOL,
            "d": {"protocol": "udp", "data": {"address": ip, "port": port, "mode": mode}},
        }

        await self.send_as_json(payload)

    async def client_connect(self) -> None:
        payload = {"op": self.CLIENT_CONNECT, "d": {"audio_ssrc": self._connection.ssrc}}

        await self.send_as_json(payload)

    async def speak(self, state: SpeakingState = SpeakingState.voice) -> None:
        payload = {
            "op": self.SPEAKING,
            "d": {
                "speaking": int(state),
                "delay": 0,
                "ssrc": self._connection.ssrc,
            },
        }

        await self.send_as_json(payload)

    async def _heartbeat_loop(self) -> None:
        timeout = self.heartbeat_interval * random.random()
        await asyncio.sleep(timeout)

        while not self.ws.closed:
            await self.heartbeat()

            self._heartbeat_ack_received = self.loop.create_future()
            await self._heartbeat_ack_received

            await asyncio.sleep(self.heartbeat_interval)

    async def received_message(self, msg: Dict[str, Any]) -> None:
        _log.debug("Voice websocket frame received: %s", msg)
        op: int = msg["op"]
        data: Dict[str, Any] = msg["d"]

        if op == self.READY:
            await self.initial_connection(data)

        elif op == self.HEARTBEAT_ACK and self._heartbeat_ack_received:
            self._heartbeat_ack_received.set_result(None)
            ack_time = time.perf_counter()

            if len(self._latencies) > 20:
                self._latencies.clear()

            self._latencies.append(ack_time - self._heartbeat_sent_time)

        elif op == self.RESUMED:
            _log.info("Voice RESUME succeeded.")

        elif op == self.SESSION_DESCRIPTION:
            self._connection.mode = data["mode"]
            await self.load_secret_key(data)

        elif op == self.HELLO:
            self.heartbeat_interval = data["heartbeat_interval"] / 1000.0
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        if self._hook is not None:
            await self._hook(self, msg)

    async def initial_connection(self, data: Dict[str, Any]) -> None:
        state = self._connection
        state.ssrc = data["ssrc"]
        state.voice_port = data["port"]
        state.endpoint_ip = data["ip"]

        # Discover our external IP and port by asking our voice port.
        # https://discord.dev/topics/voice-connections#ip-discovery
        packet = bytearray(74)

        # > = big-endian, H = unsigned short, I = unsigned int
        struct.pack_into(">H", packet, 0, 1)  # 1 = Request
        struct.pack_into(">H", packet, 2, 70)  # 70 = Message length. A constant of 70.
        struct.pack_into(">I", packet, 4, state.ssrc)
        state.socket.sendto(packet, (state.endpoint_ip, state.voice_port))
        recv = await self.loop.sock_recv(state.socket, 74)
        _log.debug("received packet in initial_connection: %s", recv)

        # the ip is ascii starting at the 8th byte and ending at the first null
        ip_start = 8
        ip_end = recv.index(0, ip_start)
        state.ip = recv[ip_start:ip_end].decode("ascii")

        state.port = struct.unpack_from(">H", recv, len(recv) - 2)[0]
        _log.debug("detected ip: %s port: %s", state.ip, state.port)

        # there *should* always be at least one supported mode (xsalsa20_poly1305)
        modes = [mode for mode in data["modes"] if mode in self._connection.supported_modes]
        _log.debug("received supported encryption modes: %s", ", ".join(modes))

        mode = modes[0]
        await self.select_protocol(state.ip, state.port, mode)
        _log.info("selected the voice protocol for use (%s)", mode)

    @property
    def latency(self) -> float:
        """:class:`float`: Latency between a HEARTBEAT and its HEARTBEAT_ACK in seconds."""
        return self._latencies[-1] if self._latencies else float("inf")

    @property
    def average_latency(self) -> float:
        """:class:`list`: Average of last 20 HEARTBEAT latencies."""
        if not self._latencies:
            return float("inf")

        return sum(self._latencies) / len(self._latencies)

    async def load_secret_key(self, data: Dict[str, Any]) -> None:
        _log.info("received secret key for voice connection")
        self.secret_key = self._connection.secret_key = data["secret_key"]
        # Send a speak command with the "not speaking" state.
        # This also tells Discord our SSRC value, which Discord requires
        # before sending any voice data (and is the real reason why we
        # call this here).
        await self.speak(SpeakingState.none)

    async def poll_event(self) -> None:
        # This exception is handled up the chain
        msg = await asyncio.wait_for(self.ws.receive(), timeout=30.0)
        if msg.type is aiohttp.WSMsgType.TEXT:
            await self.received_message(utils.from_json(msg.data))
        elif msg.type is aiohttp.WSMsgType.ERROR:
            _log.debug("Received %s", msg)
            raise ConnectionClosed(self.ws, shard_id=None) from msg.data
        elif msg.type in (
            aiohttp.WSMsgType.CLOSED,
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSING,
        ):
            _log.debug("Received %s", msg)
            raise ConnectionClosed(self.ws, shard_id=None, code=self._close_code)

    async def close(self, code: int = 1000) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

        self._close_code = code
        await self.ws.close(code=code)
