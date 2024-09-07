# SPDX-License-Identifier: MIT

from typing import TYPE_CHECKING, Any, Callable, Coroutine, TypeVar, Union

if TYPE_CHECKING:
    from ..interactions import Interaction

    T = TypeVar("T")

    Coro = Coroutine[Any, Any, T]
    MaybeCoro = Union[T, Coro[T]]
    CoroFunc = Callable[..., Coro[Any]]
    ApplicationCheck = Callable[[Interaction], MaybeCoro[bool]]
    ApplicationHook = Callable[[Interaction], Coro[Any]]
    ApplicationErrorCallback = Callable[[Interaction, Exception], Coro[Any]]
