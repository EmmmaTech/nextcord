# SPDX-License-Identifier: MIT
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable, Dict, Generic, List, Optional, Type, TypeVar, Union

import nextcord
from nextcord.utils import MISSING

from .core import Command, Group, command, group

if TYPE_CHECKING:
    from typing_extensions import Concatenate, ParamSpec

    from ._types import Check, Coro, CoroFunc
    from .bot import AutoShardedBot, Bot
    from .context import Context

__all__ = ("Plugin",)


T = TypeVar("T")
BotT = TypeVar("BotT", bound="Union[Bot, AutoShardedBot]")
CommandT = TypeVar("CommandT", bound="Command")
GroupT = TypeVar("GroupT", bound="Group")
ContextT = TypeVar("ContextT", bound="Context")

P = ParamSpec("P") if TYPE_CHECKING else TypeVar("P")


class Plugin(nextcord.Plugin, Generic[BotT]):
    __slots__ = (
        "_commands",
        "_command_checks",
        "_listeners",
    )

    _bot: Optional[BotT]

    def __init__(
        self,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        # TODO: command attrs
    ):
        super().__init__(name=name, description=description)

        self._commands: List[Command] = []
        self._command_checks: List[Check] = []
        self._listeners: Dict[str, List[CoroFunc]] = {}

    @property
    def bot(self) -> BotT:
        """Union[:class:`.Bot`, :class:`.AutoShardedBot`] Returns the bot this plugin is attached to."""
        return self.client

    @property
    def commands(self) -> List[Command]:
        """List[:class:`.Command`] Returns the list of all commands registered to this plugin."""
        return self._commands

    @property
    def listeners(self) -> Dict[str, List[CoroFunc]]:
        """Dict[:class:`str`, List[Callable[..., Any]]] Returns a dictionary of events mapped to their respective listeners."""
        return self._listeners

    def command(
        self,
        name: str = MISSING,
        cls: Union[Type[CommandT], Type[Command[Any, P, T]]] = MISSING,
        **attrs: Any,
    ) -> Callable[[Callable[Concatenate[Context, P], Coro[Any]]], Union[Command[Any, P, T], CommandT]]:
        """Creates a :class:`.Command` from the decorated function. Equivalent to :func:`.command`."""

        def decorator(func: Any) -> Union[Command[Any, P, T], CommandT]:
            result = command(name, cls, **attrs)(func)
            self._commands.append(result)
            return result

        return decorator

    def group(
        self,
        name: str = MISSING,
        cls: Union[Type[GroupT], Type[Group[Any, P, T]]] = MISSING,
        **attrs: Any,
    ) -> Callable[[Callable[Concatenate[Context, P], Coro[Any]]], Union[Group[Any, P, T], GroupT]]:
        """Creates a :class:`.Group` from the decorated function. Equivalent to :func:`.group`."""

        def decorator(func: Any) -> Union[Group[Any, P, T], GroupT]:
            result = group(name, cls, **attrs)(func)
            self._commands.append(result)
            return result

        return decorator

    def command_check(self, func: Check) -> Check:
        """A decorator that marks a function as a check for every :class:`.Command` in this plugin.

        Parameters
        ----------
        func: :class:`Check`
            The function that will be used as a check.

        Raises
        ------
        TypeError
            The function is not a coroutine function.
        """
        if not asyncio.iscoroutinefunction(func):
            raise TypeError("Check function must be a coroutine function.")

        self._command_checks.append(func)
        return func

    def listener(self, name: str = MISSING) -> Callable[[CoroFunc], CoroFunc]:
        """A decorator that marks a function as a listener.

        Equivalent to :meth:`commands.Bot.listen`.

        Parameters
        ----------
        name: :class:`str`
            The name of the event being listened to. If not provided, it
            defaults to the function's name.

        Raises
        ------
        TypeError
            The function is not a coroutine function or a string was not passed as
            the name.
        """
        def decorator(func: CoroFunc) -> CoroFunc:
            if not asyncio.iscoroutinefunction(func):
                raise TypeError("Listener function must be a coroutine function.")

            to_assign = name or func.__name__
            if to_assign in self._listeners:
                self._listeners[to_assign].append(func)
            else:
                self._listeners[to_assign] = [func]

            return func

        return decorator

    async def load(self, bot: BotT):
        await super().load(bot)

        for cmd in self._commands:
            for check in self._command_checks:
                cmd.add_check(check)

            bot.add_command(cmd)

        for event, listeners in self._listeners.items():
            for listener in listeners:
                bot.add_listener(listener, event)

    async def unload(self):
        if self._bot is None:
            raise AttributeError("Plugin has to be loaded before unloading it!")

        # we need to copy a reference to the bot since nextcord.Plugin.unload sets it to None
        bot = self._bot

        await super().unload()

        for cmd in self._commands:
            for check in self._command_checks:
                cmd.remove_check(check)

            bot.remove_command(cmd.name)

        for event, listeners in self._listeners.items():
            for listener in listeners:
                bot.remove_listener(listener, event)
