"""The signals that diagnose a dead link must not die with it.

MadokaRssiSensor reads HA's own BLE tracker and MadokaConnectionSourceSensor
reads the live connection plus the config entry: neither needs a device
round-trip, yet both inherited CoordinatorEntity's default
available = last_update_success. So the two entities that tell you WHY the
thermostat is unreachable went unavailable at exactly the moment it became
unreachable, and the user was left with a wall of "unavailable" and the logs.

MadokaConnectionStatusSensor is the missing signal: on a dashboard, a device
that is out of range and a device whose bond was refused look identical, but
they need opposite remedies.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from pymadoka.connection import ConnectionStatus
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity

from custom_components.daikin_madoka import sensor as sensor_platform
from custom_components.daikin_madoka.const import CONF_MAC, DOMAIN
from custom_components.daikin_madoka.coordinator import (
    MadokaCoordinator,
    async_pairing_state,
)

MAC = "D0:CF:13:0F:11:F6"
SOURCE = "AA:BB:CC:11:22:33"
BLUETOOTH = "homeassistant.components.bluetooth"


def _controller() -> MagicMock:
    controller = MagicMock()
    controller.connection.address = MAC
    controller.connection.name = "Daikin"
    controller.connection.connection_status = ConnectionStatus.CONNECTED
    controller.connection.connected_source = SOURCE
    controller.temperatures.status = SimpleNamespace(indoor=23.0, outdoor=30.0)
    return controller


async def _sensors(
    hass: HomeAssistant, controller: MagicMock
) -> tuple[dict[str, Entity], MadokaCoordinator]:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_MAC: MAC})
    entry.add_to_hass(hass)
    token = config_entries.current_entry.set(entry)
    try:
        coordinator = MadokaCoordinator(hass, controller, scan_interval=60)
    finally:
        config_entries.current_entry.reset(token)
    coordinator.async_boost = AsyncMock()
    entry.runtime_data = {MAC: coordinator}

    added: list[Entity] = []
    await sensor_platform.async_setup_entry(hass, entry, added.extend)
    for entity in added:
        entity.hass = hass
    return {entity.unique_id: entity for entity in added}, coordinator


async def test_link_sensors_stay_available_while_the_poll_fails(
    hass: HomeAssistant,
) -> None:
    """The two signals a user needs to self-diagnose must never go dark."""
    by_id, coordinator = await _sensors(hass, _controller())
    coordinator.last_update_success = False

    assert by_id[f"{MAC}_rssi"].available is True
    assert by_id[f"{MAC}_connection_source"].available is True
    assert by_id[f"{MAC}_connection_status"].available is True
    # ...unlike the sensors that really do need the device to answer.
    assert by_id[f"{MAC}_indoor_temperature"].available is False


async def test_connection_source_is_enabled_by_default(hass: HomeAssistant) -> None:
    """A bond belongs to one proxy: which proxy is half of every diagnosis."""
    by_id, _ = await _sensors(hass, _controller())

    assert by_id[f"{MAC}_connection_source"].entity_registry_enabled_default is True
    assert by_id[f"{MAC}_connection_status"].entity_registry_enabled_default is True


# --- connection_status: the state that tells the failures apart -----------


async def test_status_connected(hass: HomeAssistant) -> None:
    by_id, _ = await _sensors(hass, _controller())

    assert by_id[f"{MAC}_connection_status"].native_value == "connected"


async def test_status_not_advertising(hass: HomeAssistant) -> None:
    """Out of range or powered off: no proxy can even see it."""
    controller = _controller()
    controller.connection.connection_status = ConnectionStatus.DISCONNECTED
    by_id, coordinator = await _sensors(hass, controller)
    coordinator.last_update_success = False

    with patch(f"{BLUETOOTH}.async_address_present", return_value=False):
        assert by_id[f"{MAC}_connection_status"].native_value == "not_advertising"


async def test_status_retrying(hass: HomeAssistant) -> None:
    """Visible but not connected, with nothing concluded about the bond."""
    controller = _controller()
    controller.connection.connection_status = ConnectionStatus.DISCONNECTED
    by_id, coordinator = await _sensors(hass, controller)
    coordinator.last_update_success = False

    with patch(f"{BLUETOOTH}.async_address_present", return_value=True):
        assert by_id[f"{MAC}_connection_status"].native_value == "retrying"


async def test_status_pairing_slow(hass: HomeAssistant) -> None:
    """A timeout streak: evidence of something, proof of nothing."""
    controller = _controller()
    controller.connection.connection_status = ConnectionStatus.DISCONNECTED
    by_id, coordinator = await _sensors(hass, controller)
    coordinator.last_update_success = False
    async_pairing_state(hass, MAC).backoff = True

    with patch(f"{BLUETOOTH}.async_address_present", return_value=True):
        assert by_id[f"{MAC}_connection_status"].native_value == "pairing_slow"


async def test_status_needs_pairing_outranks_everything_else(
    hass: HomeAssistant,
) -> None:
    """A proven refusal is the most specific diagnosis available."""
    controller = _controller()
    controller.connection.connection_status = ConnectionStatus.DISCONNECTED
    by_id, coordinator = await _sensors(hass, controller)
    coordinator.last_update_success = False
    state = async_pairing_state(hass, MAC)
    state.suspended = True
    state.backoff = True

    with patch(f"{BLUETOOTH}.async_address_present", return_value=False):
        assert by_id[f"{MAC}_connection_status"].native_value == "needs_pairing"


async def test_status_options_cover_every_reachable_state(
    hass: HomeAssistant,
) -> None:
    """SensorDeviceClass.ENUM rejects a state that is not declared."""
    by_id, _ = await _sensors(hass, _controller())
    sensor = by_id[f"{MAC}_connection_status"]

    assert set(sensor.options) == {
        "connected",
        "retrying",
        "pairing_slow",
        "needs_pairing",
        "not_advertising",
    }
