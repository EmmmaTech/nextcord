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
from .enums import Locale
from .permissions import Permissions
from .utils import MISSING

if TYPE_CHECKING:
    from .client import Client
    from .shard import AutoShardedClient
    from .types.checks import ApplicationCheck, Coro, CoroFunc

__all__ = ("Plugin",)


ClientT = TypeVar("ClientT", bound="Union[Client, AutoShardedClient]")


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
        "_app_command_checks",
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
        self._app_command_checks: List[ApplicationCheck] = []
        self._load_hooks: List[Callable[[], Coro[Any]]] = []
        self._unload_hooks: List[Callable[[], Coro[Any]]] = []

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

    def slash_command(
        self,
        name: Optional[str] = None,
        description: Optional[str] = None,
        *,
        name_localizations: Optional[Dict[Union[Locale, str], str]] = None,
        description_localizations: Optional[Dict[Union[Locale, str], str]] = None,
        guild_ids: Optional[Iterable[int]] = MISSING,
        dm_permission: Optional[bool] = None,
        nsfw: bool = False,
        default_member_permissions: Optional[Union[Permissions, int]] = None,
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
                dm_permission=dm_permission,
                default_member_permissions=default_member_permissions,
                nsfw=nsfw,
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
        dm_permission: Optional[bool] = None,
        default_member_permissions: Optional[Union[Permissions, int]] = None,
        nsfw: bool = False,
        force_global: bool = False,
    ):
        """Creates a User context command from the decorated function. Equivalent to :func:`.user_command`."""

        def decorator(func: CoroFunc):
            result = user_command(
                name=name,
                name_localizations=name_localizations,
                guild_ids=guild_ids,
                dm_permission=dm_permission,
                default_member_permissions=default_member_permissions,
                nsfw=nsfw,
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
        dm_permission: Optional[bool] = None,
        default_member_permissions: Optional[Union[Permissions, int]] = None,
        nsfw: bool = False,
        force_global: bool = False,
    ):
        """Creates a Message context command from the decorated function. Equivalent to :func:`.message_command`."""

        def decorator(func: CoroFunc):
            result = message_command(
                name=name,
                name_localizations=name_localizations,
                guild_ids=guild_ids,
                dm_permission=dm_permission,
                default_member_permissions=default_member_permissions,
                nsfw=nsfw,
                force_global=force_global,
            )(func)
            self._app_commands.append(result)
            return result

        return decorator

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

            bot.add_application_command(cmd)

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

        self._run_hooks(self._unload_hooks)

        self._bot = None

    def make_extension_handlers(self) -> Tuple[Callable[[ClientT], None], Callable[[ClientT], None]]:
        """Returns functions for this plugin that handle extension loading/unloading."""

        def setup(bot: ClientT):
            _ = asyncio.create_task(self.load(bot))

        def teardown(bot: ClientT):
            _ = asyncio.create_task(self.unload())

        return setup, teardown
