"""A configured thermostat must always load, connected or not.

Field incident 2026-07-26. Two thermostats came back from an HA restart
unable to connect. async_setup_entry re-raised ConfigEntryNotReady for every
failure except an already-concluded pairing refusal, so both entries sat in
setup_retry: no entities at all, therefore no Reconnect button — the button
the integration's own pairing notification tells the user to press — and a
brand-new coordinator on every retry, each performing exactly one poll, so no
failure streak could accumulate and no repair could ever fire.

The rule that follows: a device that once validated (the config flow performs
a full authenticated connect before creating the entry) can only fail at
RUNTIME, and a runtime failure is an unavailable entity, never a refused
setup. The entry loads, the coordinator stays alive, and its update_interval
drives the retries.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from pymadoka import ConnectionException, PairingRequiredError
from pymadoka.connection import ConnectionStatus
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.daikin_madoka.const import CONF_MAC, DOMAIN
from custom_components.daikin_madoka.coordinator import async_pairing_state

MAC = "D0:CF:13:0F:11:F6"
SOURCE = "AA:BB:CC:11:22:33"
BLUETOOTH = "homeassistant.components.bluetooth"


@pytest.fixture
def expected_lingering_timers() -> bool:
    """Setting the entry up pulls in the bluetooth integration, whose scanner
    schedules a device-expiry timer that outlives the test; it is HA's own
    bookkeeping, not ours (same waiver as test_config_flow)."""
    return True


def _controller() -> MagicMock:
    controller = MagicMock()
    controller.connection.address = MAC
    controller.connection.name = "Daikin"
    controller.connection.connection_status = ConnectionStatus.DISCONNECTED
    controller.connection.connected_source = SOURCE
    controller.connection.pairing_timeout_rounds = 0
    controller.info = {}
    controller.stop = AsyncMock()
    controller.read_info = AsyncMock()
    controller.update = AsyncMock()
    controller.refresh_status.return_value = {"set_point": {"cooling_set_point": 25}}
    return controller


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_MAC: MAC}, title="Salon")
    entry.add_to_hass(hass)
    return entry


def _setup_patches(controller: MagicMock, platforms: list[str]):
    return (
        patch(
            "custom_components.daikin_madoka.Controller",
            MagicMock(return_value=controller),
        ),
        patch("custom_components.daikin_madoka.async_register_card", AsyncMock()),
        patch("custom_components.daikin_madoka.COMPONENT_TYPES", platforms),
        patch(f"{BLUETOOTH}.async_address_present", return_value=True),
        patch(
            f"{BLUETOOTH}.async_scanner_by_source",
            return_value=SimpleNamespace(name="Proxy"),
        ),
    )


async def _setup(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    controller: MagicMock,
    platforms: list[str] | None = None,
) -> bool:
    p1, p2, p3, present, scanner = _setup_patches(controller, platforms or [])
    with p1, p2, p3, present, scanner:
        ok = await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return ok


async def test_connect_failure_loads_degraded(
    hass: HomeAssistant, mock_bluetooth
) -> None:
    """An ordinary connect failure no longer defers setup."""
    entry = _entry(hass)
    controller = _controller()
    controller.start = AsyncMock(side_effect=ConnectionException("device off"))

    assert await _setup(hass, entry, controller)

    assert entry.state is config_entries.ConfigEntryState.LOADED
    assert MAC in entry.runtime_data
    assert entry.runtime_data[MAC].last_update_success is False


async def test_device_not_advertising_loads_degraded(
    hass: HomeAssistant, mock_bluetooth
) -> None:
    """The commonest failure of all — out of range — must not hide the UI."""
    entry = _entry(hass)
    controller = _controller()
    controller.start = AsyncMock()

    p1, p2, p3, _present, scanner = _setup_patches(controller, [])
    with (
        p1,
        p2,
        p3,
        scanner,
        patch(f"{BLUETOOTH}.async_address_present", return_value=False),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is config_entries.ConfigEntryState.LOADED
    controller.start.assert_not_awaited()


async def test_reconnect_button_exists_on_a_device_that_never_connected(
    hass: HomeAssistant, mock_bluetooth
) -> None:
    """The whole point: the remedy the notification names must be pressable."""
    entry = _entry(hass)
    controller = _controller()
    controller.start = AsyncMock(side_effect=ConnectionException("device off"))

    assert await _setup(hass, entry, controller, platforms=["button"])

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("button", DOMAIN, f"{MAC}_reconnect")
    assert entity_id is not None
    # Unlike every other entity of a dead device, this one is usable.
    assert hass.states.get(entity_id).state != "unavailable"

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_degraded_entry_keeps_retrying_on_its_interval(
    hass: HomeAssistant, mock_bluetooth, freezer: FrozenDateTimeFactory
) -> None:
    """Loading is only half the fix: the entry must also keep trying.

    The coordinator schedules its next refresh as soon as an entity listens,
    so forwarding the platforms is what restores the retry cadence a
    setup_retry loop used to provide.
    """
    entry = _entry(hass)
    controller = _controller()
    controller.start = AsyncMock(side_effect=ConnectionException("device off"))

    assert await _setup(hass, entry, controller, platforms=["button"])
    first_attempts = controller.start.await_count
    assert first_attempts == 1

    p1, p2, p3, present, scanner = _setup_patches(controller, ["button"])
    with p1, p2, p3, present, scanner:
        freezer.tick(timedelta(seconds=61))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert controller.start.await_count > first_attempts

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_pairing_timeout_streak_still_backs_off_while_loaded(
    hass: HomeAssistant, mock_bluetooth, freezer: FrozenDateTimeFactory
) -> None:
    """P0.3's backoff drives the loaded entry's cadence, not a setup retry."""
    entry = _entry(hass)
    controller = _controller()
    # A timeout streak never convicts; it slows the poll down instead. Since
    # the 0.3.10 pin the verdict has to be stated: the constructor defaults to
    # reason="rejected", so a bare PairingRequiredError is a refusal whatever
    # the round counter says.
    controller.connection.pairing_timeout_rounds = 3
    controller.start = AsyncMock(
        side_effect=PairingRequiredError(
            MAC, [SOURCE], reason="timeout_streak", timeout_rounds=3
        )
    )

    assert await _setup(hass, entry, controller, platforms=["button"])

    coordinator = entry.runtime_data[MAC]
    assert async_pairing_state(hass, MAC).suspended is False
    assert async_pairing_state(hass, MAC).backoff is True
    assert coordinator.update_interval == timedelta(seconds=900)

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
