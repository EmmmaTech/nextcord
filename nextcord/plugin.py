# SPDX-License-Identifier: MIT
from __future__ import annotations

import asyncio
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Generic,
    Iterable,
    List,
    Optional,
    Tuple,
    TypeVar,
    Union,
)

from .application_command import (
    BaseApplicationCommand,
    message_command,
    slash_command,
    user_command,
)
from .enums import IntegrationType, InteractionContextType, Locale
from .permissions import Permissions
from .utils import MISSING

if TYPE_CHECKING:
    from .client import Client
    from .shard import AutoShardedClient
    from .types.checks import ApplicationCheck, ApplicationErrorCallback, Coro, CoroFunc

__all__ = ("Plugin",)


ClientT = TypeVar("ClientT", bound="Union[Client, AutoShardedClient]")
HookFunc = Callable[[], Coro[Any]]


# TODO: maybe just types.SimpleNamespace?
class PluginExtras(dict):
    """Represents the extra data for a plugin."""
    def __init__(self) -> None:
        super().__init__()
        self.__dict__ = self

    def __repr__(self) -> str:
        return f"<PluginAttached {super().__repr__()}>"


class Plugin(Generic[ClientT]):
    """A collection of commands, listeners, and state separated from a bot instance.

    Plugins are used to help organize state together into one file, then easily load the
    state into a bot.

    Parameters
    ----------
    name: Optional[:class:`str`]
        The name of this plugin. Useful for retrieving a plugin from another one.
    description: Optional[:class:`str`]
        The description of this plugin.

    Attributes
    ----------
    extras: :class:`PluginExtras`
        Any extra state attached with this plugin. Useful for sharing state between
        plugins or the bot.
    """

    __slots__ = (
        "_app_commands",
        "_app_command_error",
        "_app_command_checks",
        "_listeners",
        "_bot",
        "_load_hooks",
        "_unload_hooks",
        "name",
        "description",
        "extras",
    )

    def __init__(
        self,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        # TODO: app command attrs
    ):
        self.name: Optional[str] = name
        self.description: Optional[str] = description
        self.extras: PluginExtras = PluginExtras()

        self._bot: Optional[ClientT] = None
        self._app_commands: List[BaseApplicationCommand] = []
        self._app_command_error: Optional[ApplicationErrorCallback] = None
        self._app_command_checks: List[ApplicationCheck] = []
        self._listeners: Dict[str, List[CoroFunc]] = {}
        self._load_hooks: List[HookFunc] = []
        self._unload_hooks: List[HookFunc] = []

    @property
    def client(self) -> ClientT:
        """Union[:class:`Client`, :class:`AutoShardedClient`] Returns the client this plugin is attached to."""
        if self._bot is None:
            raise AttributeError("Plugin has to be loaded first before accessing the attached client/bot!")
        return self._bot

    @property
    def application_commands(self) -> List[BaseApplicationCommand]:
        """List[:class:`BaseApplicationCommand`] Returns the list of all application commands registered to this plugin."""
        return self._app_commands

    @property
    def listeners(self) -> Dict[str, List[CoroFunc]]:
        """Dict[:class:`str`, List[Callable[..., Any]]] Returns a dictionary of events mapped to their respective listeners."""
        return self._listeners

    def slash_command(
        self,
        name: Optional[str] = None,
        description: Optional[str] = None,
        *,
        name_localizations: Optional[Dict[Union[Locale, str], str]] = None,
        description_localizations: Optional[Dict[Union[Locale, str], str]] = None,
        guild_ids: Optional[Iterable[int]] = MISSING,
        default_member_permissions: Optional[Union[Permissions, int]] = None,
        nsfw: bool = False,
        integration_types: Optional[Iterable[Union[IntegrationType, int]]] = None,
        contexts: Optional[Iterable[Union[InteractionContextType, int]]] = None,
        force_global: bool = False,
    ):
        """Creates a Slash application command from the decorated function. Equivalent to :func:`.slash_command`."""

        def decorator(func: CoroFunc):
            result = slash_command(
                name=name,
                name_localizations=name_localizations,
                description=description,
                description_localizations=description_localizations,
                guild_ids=guild_ids,
                default_member_permissions=default_member_permissions,
                nsfw=nsfw,
                integration_types=integration_types,
                contexts=contexts,
                force_global=force_global,
            )(func)
            self._app_commands.append(result)
            return result

        return decorator

    def user_command(
        self,
        name: Optional[str] = None,
        *,
        name_localizations: Optional[Dict[Union[Locale, str], str]] = None,
        guild_ids: Optional[Iterable[int]] = MISSING,
        default_member_permissions: Optional[Union[Permissions, int]] = None,
        nsfw: bool = False,
        integration_types: Optional[Iterable[Union[IntegrationType, int]]] = None,
        contexts: Optional[Iterable[Union[InteractionContextType, int]]] = None,
        force_global: bool = False,
    ):
        """Creates a User context command from the decorated function. Equivalent to :func:`.user_command`."""

        def decorator(func: CoroFunc):
            result = user_command(
                name=name,
                name_localizations=name_localizations,
                guild_ids=guild_ids,
                default_member_permissions=default_member_permissions,
                nsfw=nsfw,
                integration_types=integration_types,
                contexts=contexts,
                force_global=force_global,
            )(func)
            self._app_commands.append(result)
            return result

        return decorator

    def message_command(
        self,
        name: Optional[str] = None,
        *,
        name_localizations: Optional[Dict[Union[Locale, str], str]] = None,
        guild_ids: Optional[Iterable[int]] = MISSING,
        default_member_permissions: Optional[Union[Permissions, int]] = None,
        nsfw: bool = False,
        integration_types: Optional[Iterable[Union[IntegrationType, int]]] = None,
        contexts: Optional[Iterable[Union[InteractionContextType, int]]] = None,
        force_global: bool = False,
    ):
        """Creates a Message context command from the decorated function. Equivalent to :func:`.message_command`."""

        def decorator(func: CoroFunc):
            result = message_command(
                name=name,
                name_localizations=name_localizations,
                guild_ids=guild_ids,
                default_member_permissions=default_member_permissions,
                nsfw=nsfw,
                integration_types=integration_types,
                contexts=contexts,
                force_global=force_global,
            )(func)
            self._app_commands.append(result)
            return result

        return decorator

    def load_hook(self, func: HookFunc) -> HookFunc:
        """A decorator that marks a function as a load hook for this plugin.

        Parameters
        ----------
        func: Callable[[], Any]
            The function that will be used as a load hook.

        Raises
        ------
        TypeError
            The function is not a coroutine function.
        """
        if not asyncio.iscoroutinefunction(func):
            raise TypeError("Load hook must be a coroutine function.")

        self._load_hooks.append(func)
        return func

    def unload_hook(self, func: HookFunc) -> HookFunc:
        """A decorator that marks a function as a unload hook for this plugin.

        Parameters
        ----------
        func: Callable[[], Any]
            The function that will be used as a unload hook.

        Raises
        ------
        TypeError
            The function is not a coroutine function.
        """
        if not asyncio.iscoroutinefunction(func):
            raise TypeError("Unload hook must be a coroutine function.")

        self._unload_hooks.append(func)
        return func

    def application_command_check(self, func: ApplicationCheck) -> ApplicationCheck:
        """A decorator that marks a function as a check for every application command in this plugin.

        Parameters
        ----------
        func: :class:`ApplicationCheck`
            The function that will be used as a check.

        Raises
        ------
        TypeError
            The function is not a coroutine function.
        """
        if not asyncio.iscoroutinefunction(func):
            raise TypeError("Check function must be a coroutine function.")

        self._app_command_checks.append(func)
        return func

    def on_application_command_error(self, func: ApplicationErrorCallback) -> ApplicationErrorCallback:
        """A decorator that marks a function as a error handler for every application command in this plugin.

        Parameters
        ----------
        func: :class:`ApplicationErrorCallback`
            The function that will be used as the error handler.

        Raises
        ------
        TypeError
            The function is not a coroutine function.
        """
        if not asyncio.iscoroutinefunction(func):
            raise TypeError("Error handler must be a coroutine function.")

        self._app_command_error = func
        return func

    def listener(self, name: str = MISSING) -> Callable[[CoroFunc], CoroFunc]:
        """A decorator that marks a function as a listener.

        Equivalent to :meth:`Client.listen`.

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

    def _run_hooks(self, hooks: List[Callable[[], Coro[Any]]]) -> None:
        for hook in hooks:
            # storing a reference to the created task wouldn't be helpful
            asyncio.create_task(hook(), name=f"nextcord plugin-hook: {hook.__name__}")  # noqa: RUF006

    async def load(self, bot: ClientT):
        """Loads the contents of this plugin into the provided bot/client."""
        if self._bot is not None:
            raise AttributeError("Plugin cannot be loaded more than once!")

        self._bot = bot

        for cmd in self._app_commands:
            for check in self._app_command_checks:
                cmd.add_check(check)

            if cmd.error_callback is None:
                cmd.error_callback = self._app_command_error

            bot.add_application_command(cmd)

        for event, listeners in self._listeners.items():
            for listener in listeners:
                bot.add_listener(listener, event)

        self._run_hooks(self._load_hooks)

    async def unload(self):
        """Unloads the contents of this plugin from the attached bot/client."""
        if self._bot is None:
            raise AttributeError("Plugin has to be loaded before unloading it!")

        for cmd in self._app_commands:
            for check in self._app_command_checks:
                cmd.remove_check(check)

            # TODO: use remove_application_command from Client, when added
            self._bot._connection.remove_application_command(cmd)

        for event, listeners in self._listeners.items():
            for listener in listeners:
                self._bot.remove_listener(listener, event)

        self._run_hooks(self._unload_hooks)

        self._bot = None

    def make_extension_handlers(self) -> Tuple[Callable[[ClientT], None], Callable[[ClientT], None]]:
        """Returns functions for this plugin that handle extension loading/unloading."""

        def setup(bot: ClientT):
            asyncio.create_task(self.load(bot))  # noqa: RUF006

        def teardown(_: ClientT):
            asyncio.create_task(self.unload())  # noqa: RUF006

        return setup, teardown
