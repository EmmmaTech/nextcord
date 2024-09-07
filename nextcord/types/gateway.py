# SPDX-License-Identifier: MIT

from typing import Any, Dict, Literal, Union, TypedDict

from typing_extensions import NotRequired


class SessionStartLimit(TypedDict):
    total: int
    remaining: int
    reset_after: int
    max_concurrency: int


class Gateway(TypedDict):
    url: str


class GatewayBot(Gateway):
    shards: int
    session_start_limit: SessionStartLimit


class DispatchEvent(TypedDict):
    op: Literal[0]
    t: str
    s: NotRequired[int]
    d: Dict[str, Any]


class HeartbeatEvent(TypedDict):
    op: Literal[1]


class ReconnectEvent(TypedDict):
    op: Literal[7]


class InvalidSessionEvent(TypedDict):
    op: Literal[9]
    d: bool


class _HelloEventPayload(TypedDict):
    heartbeat_interval: int


class HelloEvent(TypedDict):
    op: Literal[10]
    d: _HelloEventPayload


class HeartbeatAckEvent(TypedDict):
    op: Literal[11]


ReceivableGatewayEvent = Union[DispatchEvent, HeartbeatEvent, ReconnectEvent, InvalidSessionEvent, HelloEvent, HeartbeatAckEvent]
