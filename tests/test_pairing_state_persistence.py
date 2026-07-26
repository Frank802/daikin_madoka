"""The verdict about a bond must outlive a rebuild, a reload and a restart.

Two related defects, one field incident (2026-07-26):

1. _fail_count was an instance attribute of the coordinator. HA builds a
   fresh coordinator on every setup retry and each one performs exactly one
   refresh, so the counter could never reach UNREACHABLE_THRESHOLD: the
   device_unreachable repair was unreachable code. Two dead thermostats
   produced zero notifications.
2. The whole pairing verdict lived in hass.data only, so an HA restart
   silently reset a quarantine that had taken hours to establish and, worse,
   it was never purged, so a stale suspension could haunt a delete-and-re-add,
   the escape hatch of last resort.

The rules: the counter and the verdict live in the shared per-MAC state; the
state is mirrored into the config entry (so it survives a restart) and dropped
when the device or the entry goes away (so it cannot haunt a re-add). A
success always rewrites the durable copy, so persistence can never resurrect
a quarantine that a working connection cleared.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from pymadoka import ConnectionException, PairingRequiredError
from pymadoka.connection import ConnectionStatus
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.daikin_madoka import async_remove_config_entry_device
from custom_components.daikin_madoka.const import (
    CONF_MAC,
    CONF_PAIRING_STATE,
    DOMAIN,
)
from custom_components.daikin_madoka.coordinator import (
    PAIRING_STATE_KEY,
    UNREACHABLE_THRESHOLD,
    MadokaCoordinator,
    async_forget_pairing_state,
    async_pairing_state,
    async_restore_pairing_state,
)

MAC = "D0:CF:13:0F:11:F6"
SOURCE = "AA:BB:CC:11:22:33"
BLUETOOTH = "homeassistant.components.bluetooth"


def _controller() -> MagicMock:
    controller = MagicMock()
    controller.connection.address = MAC
    controller.connection.name = "Daikin"
    controller.connection.connection_status = ConnectionStatus.DISCONNECTED
    controller.connection.connected_source = SOURCE
    controller.connection.pairing_timeout_rounds = 0
    controller.update = AsyncMock()
    controller.refresh_status.return_value = {"set_point": {"cooling_set_point": 25}}
    return controller


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_MAC: MAC})
    entry.add_to_hass(hass)
    return entry


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


def _simulate_restart(hass: HomeAssistant) -> None:
    """Everything in hass.data is gone; only the config entry survives."""
    hass.data.pop(PAIRING_STATE_KEY, None)


# --------------------------------------------------------------------------
# 1. The failure counter accumulates across coordinator rebuilds
# --------------------------------------------------------------------------


async def test_fail_count_accumulates_across_coordinator_rebuilds(
    hass: HomeAssistant,
) -> None:
    """One refresh per rebuild used to mean the counter was always 1."""
    entry = _entry(hass)

    for _ in range(UNREACHABLE_THRESHOLD):
        controller = _controller()
        controller.start = AsyncMock(side_effect=ConnectionException("device off"))
        coordinator = _coordinator(hass, entry, controller)
        present, scanner = _patched_bluetooth()
        with present, scanner:
            await coordinator.async_refresh()

    assert async_pairing_state(hass, MAC).fail_count == UNREACHABLE_THRESHOLD


async def test_device_unreachable_repair_fires_for_a_device_never_connected(
    hass: HomeAssistant,
) -> None:
    """The repair that never fired in the field."""
    entry = _entry(hass)

    for _ in range(UNREACHABLE_THRESHOLD):
        controller = _controller()
        controller.start = AsyncMock(side_effect=ConnectionException("device off"))
        coordinator = _coordinator(hass, entry, controller)
        present, scanner = _patched_bluetooth()
        with present, scanner:
            await coordinator.async_refresh()

    assert coordinator.unreachable_issue_active is True
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"unreachable_{MAC}") is not None


async def test_successful_poll_resets_the_shared_counter(
    hass: HomeAssistant,
) -> None:
    """A recovery must clear the streak for everybody, not just this object."""
    entry = _entry(hass)
    controller = _controller()
    controller.start = AsyncMock(side_effect=ConnectionException("device off"))
    coordinator = _coordinator(hass, entry, controller)
    present, scanner = _patched_bluetooth()

    with present, scanner:
        await coordinator.async_refresh()
        assert async_pairing_state(hass, MAC).fail_count == 1

        controller.connection.connection_status = ConnectionStatus.CONNECTED
        await coordinator.async_refresh()

    assert async_pairing_state(hass, MAC).fail_count == 0


# --------------------------------------------------------------------------
# 2. The verdict survives a restart
# --------------------------------------------------------------------------


async def test_quarantine_survives_a_restart(hass: HomeAssistant) -> None:
    """A diagnosis reached at 2am must not have to be re-derived at 8am."""
    entry = _entry(hass)
    controller = _controller()
    controller.start = AsyncMock(side_effect=PairingRequiredError(MAC, [SOURCE]))
    coordinator = _coordinator(hass, entry, controller)
    present, scanner = _patched_bluetooth()

    with present, scanner:
        await coordinator.async_refresh()

    assert entry.data[CONF_PAIRING_STATE][MAC]["suspended"] is True

    _simulate_restart(hass)
    async_restore_pairing_state(hass, entry)

    restored = async_pairing_state(hass, MAC)
    assert restored.suspended is True
    # The proxies that refused are restored too, so the repair can name them.
    assert restored.last_error is not None
    assert restored.last_error.tried_sources == [SOURCE]
    # A window is permission for one attempt by a human who is present: it
    # must never be reopened by a restart.
    assert restored.pairing_window is False


async def test_fail_count_survives_a_restart(hass: HomeAssistant) -> None:
    """A device that was dead before the restart is still dead after it."""
    entry = _entry(hass)
    controller = _controller()
    controller.start = AsyncMock(side_effect=ConnectionException("device off"))
    coordinator = _coordinator(hass, entry, controller)
    present, scanner = _patched_bluetooth()

    with present, scanner:
        await coordinator.async_refresh()
        await coordinator.async_refresh()

    _simulate_restart(hass)
    async_restore_pairing_state(hass, entry)

    assert async_pairing_state(hass, MAC).fail_count == 2


async def test_a_successful_connect_erases_the_persisted_quarantine(
    hass: HomeAssistant,
) -> None:
    """Persistence must never resurrect a verdict a success already cleared."""
    entry = _entry(hass)
    controller = _controller()
    controller.start = AsyncMock(side_effect=PairingRequiredError(MAC, [SOURCE]))
    coordinator = _coordinator(hass, entry, controller)
    present, scanner = _patched_bluetooth()

    with present, scanner:
        await coordinator.async_refresh()
        assert CONF_PAIRING_STATE in entry.data

        controller.connection.connection_status = ConnectionStatus.CONNECTED
        await coordinator.async_refresh()

    assert CONF_PAIRING_STATE not in entry.data

    _simulate_restart(hass)
    async_restore_pairing_state(hass, entry)
    assert async_pairing_state(hass, MAC).suspended is False


async def test_restore_never_overwrites_a_live_verdict(hass: HomeAssistant) -> None:
    """Memory wins: a reload must not undo what just happened in memory."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_MAC: MAC, CONF_PAIRING_STATE: {MAC: {"suspended": True}}},
    )
    entry.add_to_hass(hass)
    live = async_pairing_state(hass, MAC)
    live.suspended = False

    async_restore_pairing_state(hass, entry)

    assert async_pairing_state(hass, MAC).suspended is False


async def test_restore_ignores_malformed_stored_state(hass: HomeAssistant) -> None:
    """Hand-edited storage must not break setup."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_MAC: MAC, CONF_PAIRING_STATE: {MAC: "nonsense"}},
    )
    entry.add_to_hass(hass)

    async_restore_pairing_state(hass, entry)

    assert MAC not in (hass.data.get(PAIRING_STATE_KEY) or {})


# --------------------------------------------------------------------------
# 3. ...and is purged when the device or the entry goes away
# --------------------------------------------------------------------------


async def test_removing_a_device_purges_its_verdict(hass: HomeAssistant) -> None:
    """A replacement thermostat must not inherit its predecessor sentence."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_MAC: MAC, CONF_PAIRING_STATE: {MAC: {"suspended": True}}},
    )
    entry.add_to_hass(hass)
    async_restore_pairing_state(hass, entry)
    entry.runtime_data = {}
    device_entry = SimpleNamespace(identifiers={(DOMAIN, MAC)})

    assert await async_remove_config_entry_device(hass, entry, device_entry) is True

    assert MAC not in (hass.data.get(PAIRING_STATE_KEY) or {})
    assert CONF_PAIRING_STATE not in entry.data


async def test_unload_drops_the_live_copy_but_keeps_the_durable_one(
    hass: HomeAssistant,
) -> None:
    """A reload rehydrates; a delete takes the entry (and the verdict) with it."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_MAC: MAC, CONF_PAIRING_STATE: {MAC: {"suspended": True}}},
    )
    entry.add_to_hass(hass)
    async_restore_pairing_state(hass, entry)

    async_forget_pairing_state(hass, entry, MAC, persisted=False)

    assert MAC not in (hass.data.get(PAIRING_STATE_KEY) or {})
    assert entry.data[CONF_PAIRING_STATE][MAC]["suspended"] is True
