"""The candidates callback must be total, or the anti-pairing policy is void.

Verified against pymadoka 0.3.9 (connection.py:276-284): if candidates_callback
raises, the library logs it and silently falls back to _connect_via_ha_single().
That fallback lets habluetooth's scorer choose ANY path, retries three times
(so it can hop between proxies mid-connect) and calls pair() unconditionally -
i.e. exactly the unattended auto-pairing that jammed four thermostats in the
field, reachable through a single stray exception in our own code.

The integration cannot change the library's fallback, so it makes the fallback
unreachable instead: the callback catches everything and returns an empty list.
The library reports an empty list as DeviceUnreachableError, which surfaces as
an ordinary failed poll and touches no radio at all. That is the safe failure
mode: a wrong "unreachable" costs one poll interval, a wrong pairing salvo
costs a trip to the thermostat and a Bluetooth toggle.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pymadoka.connection import Connection
from pymadoka.errors import DeviceUnreachableError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant

from custom_components.daikin_madoka.const import (
    CONF_BONDED_SOURCES,
    CONF_MAC,
    DOMAIN,
)

MAC = "D0:CF:13:0F:11:F6"
PROXY = "AA:BB:CC:11:22:33"
BLUETOOTH = "homeassistant.components.bluetooth"


@pytest.fixture
def expected_lingering_timers() -> bool:
    """Setting the entry up pulls in HA's bluetooth integration, whose scanner
    schedules a device-expiry timer that outlives the test (same waiver as
    test_config_flow / test_degraded_load)."""
    return True


def _controller() -> MagicMock:
    from pymadoka.connection import ConnectionStatus

    controller = MagicMock()
    controller.connection.address = MAC
    controller.connection.name = "Daikin"
    controller.connection.connection_status = ConnectionStatus.DISCONNECTED
    controller.connection.connected_source = PROXY
    controller.connection.pairing_timeout_rounds = 0
    controller.info = {}
    controller.start = AsyncMock()
    controller.stop = AsyncMock()
    controller.read_info = AsyncMock()
    controller.update = AsyncMock()
    controller.refresh_status.return_value = {}
    return controller


async def _captured_callback(hass: HomeAssistant, entry: MockConfigEntry):
    """Set the entry up and return the callback handed to pymadoka."""
    controller = MagicMock(return_value=_controller())
    with (
        patch("custom_components.daikin_madoka.Controller", controller),
        patch("custom_components.daikin_madoka.async_register_card", AsyncMock()),
        patch("custom_components.daikin_madoka.COMPONENT_TYPES", []),
        patch(f"{BLUETOOTH}.async_address_present", return_value=False),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return controller.call_args.kwargs["candidates_callback"]


async def test_the_callback_never_raises(hass: HomeAssistant) -> None:
    """Any failure inside the builder must stay inside the builder."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_MAC: MAC, CONF_BONDED_SOURCES: [PROXY]}
    )
    entry.add_to_hass(hass)
    callback = await _captured_callback(hass, entry)

    with patch(
        "custom_components.daikin_madoka.build_candidates",
        side_effect=RuntimeError("scanner registry went away"),
    ):
        assert callback() == []

    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_raising_builder_cannot_reach_the_auto_pairing_fallback(
    hass: HomeAssistant,
) -> None:
    """The consequence that matters, checked against the real library.

    An empty list is reported as DeviceUnreachableError; the single-path
    fallback - the one that pairs with whatever proxy HA picks - is never
    entered.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_MAC: MAC, CONF_BONDED_SOURCES: [PROXY]}
    )
    entry.add_to_hass(hass)
    callback = await _captured_callback(hass, entry)

    connection = Connection(MAC, None, hass=hass, candidates_callback=callback)
    fallback = AsyncMock()
    connection._connect_via_ha_single = fallback

    with (
        patch(
            "custom_components.daikin_madoka.build_candidates",
            side_effect=RuntimeError("scanner registry went away"),
        ),
        pytest.raises(DeviceUnreachableError),
    ):
        await connection._connect_via_ha()

    fallback.assert_not_awaited()

    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_healthy_builder_still_drives_the_candidates_path(
    hass: HomeAssistant,
) -> None:
    """The guard must not swallow the normal case along with the failures."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_MAC: MAC, CONF_BONDED_SOURCES: [PROXY]}
    )
    entry.add_to_hass(hass)
    callback = await _captured_callback(hass, entry)
    ble_device = SimpleNamespace(details={"source": PROXY}, name="Daikin")

    with patch(
        "custom_components.daikin_madoka.build_candidates",
        return_value=[ble_device],
    ):
        assert callback() == [ble_device]

    await hass.config_entries.async_unload(entry.entry_id)


async def test_the_config_flow_callback_is_total_too(hass: HomeAssistant) -> None:
    """The attended flow uses its own callback; it needs the same guarantee.

    Here the fallback would not even be wrong about a human being present - but
    it would still rescore and hop between proxies on its three attempts, which
    is how an unbonded proxy ends up holding the BRC1H's single central slot.
    """
    from custom_components.daikin_madoka.config_flow import FlowHandler

    flow = FlowHandler()
    flow.hass = hass
    captured: dict = {}

    class _Recorder:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            self.connection = SimpleNamespace(
                connection_status=None, connected_source=None
            )

        async def start(self):
            raise RuntimeError("not reached")

        async def stop(self):
            return None

    with (
        patch("pymadoka.Controller", _Recorder),
        patch(
            "custom_components.daikin_madoka.util.build_candidates",
            side_effect=RuntimeError("scanner registry went away"),
        ),
    ):
        await flow._async_validate_device(MAC)
        assert captured["candidates_callback"]() == []
