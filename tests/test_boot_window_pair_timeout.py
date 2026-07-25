"""Slow-but-valid bonds must not be falsely quarantined at boot.

Field observation 2026-07-25. On an HA restart every Madoka entry reconnects
at once through a handful of ESPHome proxies. A congested proxy encrypts a
perfectly valid SMP bond slowly, and the automatic first connect uses the
tight 8s steady-state pairing budget (pymadoka DEFAULT_PAIR_TIMEOUT). Slow
encryption overruns it, the library reads it as an all-paths-timed-out round,
and a device reachable through only that one proxy accumulates the streak on
every poll until PairingRequiredError fires and the coordinator quarantines it
indefinitely — even though the bond was intact all along.

The fix: for the first connect(s) after setup (the congested boot window) the
coordinator widens the pairing budget the same way async_reconnect does for a
user-initiated pairing window, so a slow-but-valid bond completes instead of
timing out. It reverts to the tight default after the first successful connect,
so steady-state reconnects stay fast. A genuine auth rejection still raises
PairingRequiredError immediately and still quarantines.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from pymadoka import PairingRequiredError
from pymadoka.connection import ConnectionStatus
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.core import HomeAssistant

from custom_components.daikin_madoka.const import (
    BOOT_PAIR_TIMEOUT,
    CONF_MAC,
    DOMAIN,
)
from custom_components.daikin_madoka.coordinator import (
    MadokaCoordinator,
    async_pairing_state,
)

MAC = "D0:CF:13:0F:11:F6"
SOURCE = "AA:BB:CC:11:22:33"
DEFAULT_PAIR_TIMEOUT = 8.0

BLUETOOTH = "homeassistant.components.bluetooth"


def _disconnected_controller() -> MagicMock:
    controller = MagicMock()
    controller.connection.address = MAC
    controller.connection.name = "Daikin"
    controller.connection.connection_status = ConnectionStatus.DISCONNECTED
    controller.connection.connected_source = SOURCE
    controller.connection.pair_timeout = DEFAULT_PAIR_TIMEOUT
    controller.update = AsyncMock()
    controller.refresh_status.return_value = {"set_point": {"cooling_set_point": 25}}
    return controller


def _coordinator(
    hass: HomeAssistant, entry: MockConfigEntry, controller: MagicMock
) -> MadokaCoordinator:
    token = config_entries.current_entry.set(entry)
    try:
        return MadokaCoordinator(hass, controller, scan_interval=60)
    finally:
        config_entries.current_entry.reset(token)


def _patched_bluetooth():
    return (
        patch(f"{BLUETOOTH}.async_address_present", return_value=True),
        patch(
            f"{BLUETOOTH}.async_scanner_by_source",
            return_value=SimpleNamespace(name="Proxy"),
        ),
    )


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_MAC: MAC})
    entry.add_to_hass(hass)
    return entry


async def test_first_connect_uses_the_wide_boot_pair_timeout(
    hass: HomeAssistant,
) -> None:
    """The congested boot window gets the wide budget, not the tight 8s."""
    entry = _entry(hass)
    controller = _disconnected_controller()
    seen = []

    async def _connect() -> None:
        seen.append(controller.connection.pair_timeout)
        controller.connection.connection_status = ConnectionStatus.CONNECTED

    controller.start = AsyncMock(side_effect=_connect)
    coordinator = _coordinator(hass, entry, controller)

    present, scanner = _patched_bluetooth()
    with present, scanner:
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert seen == [BOOT_PAIR_TIMEOUT]


async def test_reverts_to_default_after_the_first_successful_connect(
    hass: HomeAssistant,
) -> None:
    """Steady-state reconnects must go back to the tight budget."""
    entry = _entry(hass)
    controller = _disconnected_controller()
    seen = []

    async def _connect() -> None:
        seen.append(controller.connection.pair_timeout)
        controller.connection.connection_status = ConnectionStatus.CONNECTED

    controller.start = AsyncMock(side_effect=_connect)
    coordinator = _coordinator(hass, entry, controller)

    present, scanner = _patched_bluetooth()
    with present, scanner:
        # Boot connect (wide), then a later poll after the link dropped.
        await coordinator.async_refresh()
        controller.connection.connection_status = ConnectionStatus.DISCONNECTED
        await coordinator.async_refresh()

    assert seen == [BOOT_PAIR_TIMEOUT, DEFAULT_PAIR_TIMEOUT]
    assert controller.connection.pair_timeout == DEFAULT_PAIR_TIMEOUT


async def test_slow_but_valid_bond_is_not_quarantined_at_boot(
    hass: HomeAssistant,
) -> None:
    """A bond that needs more than 8s to encrypt must connect, not quarantine.

    Modelled at the coordinator boundary: on the tight budget the library
    would conclude PairingRequiredError (the streak of slow-encryption
    timeouts), but on the wide boot budget the same bond completes.
    """
    entry = _entry(hass)
    controller = _disconnected_controller()

    async def _connect() -> None:
        if controller.connection.pair_timeout < BOOT_PAIR_TIMEOUT:
            raise PairingRequiredError(MAC, [SOURCE])
        controller.connection.connection_status = ConnectionStatus.CONNECTED

    controller.start = AsyncMock(side_effect=_connect)
    coordinator = _coordinator(hass, entry, controller)

    present, scanner = _patched_bluetooth()
    with present, scanner:
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert async_pairing_state(hass, MAC).suspended is False


async def test_genuine_auth_rejection_still_quarantines_during_boot(
    hass: HomeAssistant,
) -> None:
    """The wider budget must not weaken the real dead-bond path."""
    entry = _entry(hass)
    controller = _disconnected_controller()
    controller.start = AsyncMock(side_effect=PairingRequiredError(MAC, [SOURCE]))
    coordinator = _coordinator(hass, entry, controller)

    present, scanner = _patched_bluetooth()
    with present, scanner:
        await coordinator.async_refresh()

    assert not coordinator.last_update_success
    assert async_pairing_state(hass, MAC).suspended is True


async def test_boot_window_does_not_override_an_open_pairing_window(
    hass: HomeAssistant,
) -> None:
    """A user-opened pairing window keeps its 60s human budget."""
    from custom_components.daikin_madoka.const import PAIRING_WINDOW_TIMEOUT

    entry = _entry(hass)
    controller = _disconnected_controller()
    controller.start = AsyncMock(side_effect=PairingRequiredError(MAC, [SOURCE]))
    controller.stop = AsyncMock()
    coordinator = _coordinator(hass, entry, controller)

    present, scanner = _patched_bluetooth()
    with present, scanner, patch(
        "custom_components.daikin_madoka.coordinator.asyncio.sleep", AsyncMock()
    ):
        await coordinator.async_reconnect()

    # The boot connect ran inside the pairing window: it must not have
    # clobbered the wider human budget back down to the boot value.
    assert controller.connection.pair_timeout == PAIRING_WINDOW_TIMEOUT

    await coordinator.async_shutdown()
