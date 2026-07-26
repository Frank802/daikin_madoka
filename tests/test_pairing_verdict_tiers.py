"""PairingRequiredError means two different things; treat them differently.

pymadoka raises PairingRequiredError both when a path explicitly REJECTED the
authenticated bond (proof that a human must re-pair) and when every path
merely TIMED OUT for three rounds (an inference, which congestion alone can
produce). The two raises share one constructor, so message and attributes are
byte-identical; the only side channel is the public round counter, reset to 0
by the rejection raise and left at >= PAIRING_TIMEOUT_ROUNDS by the streak.

Field incident 2026-07-26: convicting on both is what falsely quarantined a
thermostat whose bond was intact (Parents) - a manual Reconnect restored it
instantly. Ignoring timeouts is not an option either: a genuinely dead BRC1H
bond fails by silent timeout, so ignoring them recreates the v3.6.0 prompt
storm. Hence three tiers:

1. explicit rejection -> suspend + ERROR repair (a human is proven necessary);
2. timeout streak -> no conviction, a long capped poll interval and a
   WARNING repair that suggests rather than accuses;
3. anything else -> unchanged retry cadence.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from pymadoka import PairingRequiredError
from pymadoka.connection import ConnectionStatus
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.daikin_madoka.const import (
    CONF_MAC,
    DOMAIN,
    TIMEOUT_BACKOFF_INTERVAL_S,
)
from custom_components.daikin_madoka.coordinator import (
    MadokaCoordinator,
    async_pairing_state,
)

MAC = "D0:CF:13:0F:11:F6"
SOURCE = "AA:BB:CC:11:22:33"
STREAK = 3

BLUETOOTH = "homeassistant.components.bluetooth"


def _controller(rounds: int | None = 0) -> MagicMock:
    """Disconnected controller whose connect raises PairingRequiredError.

    rounds is what the library leaves on connection.pairing_timeout_rounds at
    the moment it raises: 0 after a proven rejection, >= 3 after a streak.
    None models a future library that dropped the property entirely.
    """
    controller = MagicMock()
    controller.connection.address = MAC
    controller.connection.name = "Daikin"
    controller.connection.connection_status = ConnectionStatus.DISCONNECTED
    controller.connection.connected_source = SOURCE
    controller.connection.pair_timeout = 8.0
    if rounds is None:
        del controller.connection.pairing_timeout_rounds
    else:
        controller.connection.pairing_timeout_rounds = rounds
    controller.start = AsyncMock(side_effect=PairingRequiredError(MAC, [SOURCE]))
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
            return_value=SimpleNamespace(name="Proxy Salon"),
        ),
    )


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_MAC: MAC})
    entry.add_to_hass(hass)
    return entry


# --------------------------------------------------------------------------
# Tier 1: an explicit rejection is proof, and still convicts
# --------------------------------------------------------------------------


async def test_explicit_rejection_quarantines_immediately(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    controller = _controller(rounds=0)
    coordinator = _coordinator(hass, entry, controller)

    present, scanner = _patched_bluetooth()
    with present, scanner:
        await coordinator.async_refresh()

    registry = ir.async_get(hass)
    issue = registry.async_get_issue(DOMAIN, f"pairing_required_{MAC}")
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR
    assert registry.async_get_issue(DOMAIN, f"pairing_slow_{MAC}") is None
    assert async_pairing_state(hass, MAC).suspended is True
    assert coordinator.update_interval == timedelta(seconds=60)


# --------------------------------------------------------------------------
# Tier 2: a timeout streak is an inference - back off, do not convict
# --------------------------------------------------------------------------


async def test_timeout_streak_does_not_convict(hass: HomeAssistant) -> None:
    """No suspension, no accusation: a congested path is not a dead bond."""
    entry = _entry(hass)
    controller = _controller(rounds=STREAK)
    coordinator = _coordinator(hass, entry, controller)

    present, scanner = _patched_bluetooth()
    with present, scanner:
        await coordinator.async_refresh()

    registry = ir.async_get(hass)
    assert not coordinator.last_update_success
    assert async_pairing_state(hass, MAC).suspended is False
    assert registry.async_get_issue(DOMAIN, f"pairing_required_{MAC}") is None


async def test_timeout_streak_raises_a_warning_repair(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    controller = _controller(rounds=STREAK)
    coordinator = _coordinator(hass, entry, controller)

    present, scanner = _patched_bluetooth()
    with present, scanner:
        await coordinator.async_refresh()

    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"pairing_slow_{MAC}")
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == "pairing_slow"
    assert issue.translation_placeholders == {
        "device": "Daikin",
        "proxies": "Proxy Salon",
    }
    assert coordinator.pairing_slow_issue_active is True


async def test_timeout_streak_lengthens_the_poll_interval(
    hass: HomeAssistant,
) -> None:
    """Never ignored: SMP attempts keep going but at a capped, slow cadence."""
    entry = _entry(hass)
    controller = _controller(rounds=STREAK)
    coordinator = _coordinator(hass, entry, controller)

    present, scanner = _patched_bluetooth()
    with present, scanner:
        await coordinator.async_refresh()

    assert coordinator.update_interval == timedelta(seconds=TIMEOUT_BACKOFF_INTERVAL_S)


async def test_backoff_still_probes_the_device(hass: HomeAssistant) -> None:
    """Unlike a quarantine, the next poll is allowed to try again."""
    entry = _entry(hass)
    controller = _controller(rounds=STREAK)
    coordinator = _coordinator(hass, entry, controller)

    present, scanner = _patched_bluetooth()
    with present, scanner:
        await coordinator.async_refresh()
        await coordinator.async_refresh()

    assert controller.start.await_count == 2


async def test_a_success_restores_the_interval_and_clears_the_repair(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    controller = _controller(rounds=STREAK)
    coordinator = _coordinator(hass, entry, controller)

    present, scanner = _patched_bluetooth()
    with present, scanner:
        await coordinator.async_refresh()
        assert coordinator.update_interval == timedelta(
            seconds=TIMEOUT_BACKOFF_INTERVAL_S
        )

        controller.connection.connection_status = ConnectionStatus.CONNECTED
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert coordinator.update_interval == timedelta(seconds=60)
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"pairing_slow_{MAC}") is None
    assert coordinator.pairing_slow_issue_active is False


async def test_backoff_suppresses_the_unreachable_repair(
    hass: HomeAssistant,
) -> None:
    """Power/range advice would contradict the pairing warning on screen."""
    entry = _entry(hass)
    controller = _controller(rounds=STREAK)
    coordinator = _coordinator(hass, entry, controller)

    present, scanner = _patched_bluetooth()
    with present, scanner:
        for _ in range(6):
            await coordinator.async_refresh()

    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, f"pairing_slow_{MAC}") is not None
    assert registry.async_get_issue(DOMAIN, f"unreachable_{MAC}") is None


# --------------------------------------------------------------------------
# The side channel is fragile: an unreadable counter must not convict
# --------------------------------------------------------------------------


async def test_a_missing_round_counter_takes_the_safe_branch(
    hass: HomeAssistant,
) -> None:
    """Without evidence we never accuse: back off, do not quarantine."""
    entry = _entry(hass)
    controller = _controller(rounds=None)
    coordinator = _coordinator(hass, entry, controller)

    present, scanner = _patched_bluetooth()
    with present, scanner:
        await coordinator.async_refresh()

    registry = ir.async_get(hass)
    assert async_pairing_state(hass, MAC).suspended is False
    assert registry.async_get_issue(DOMAIN, f"pairing_required_{MAC}") is None
    assert registry.async_get_issue(DOMAIN, f"pairing_slow_{MAC}") is not None


async def test_backoff_survives_a_coordinator_rebuild(hass: HomeAssistant) -> None:
    """A setup retry rebuilds everything; the slow cadence must not reset.

    This is the same lesson as the timeout streak itself: HA builds a fresh
    Controller and coordinator on every ConfigEntryNotReady retry, so any
    protection held on those objects evaporates and the device gets hammered
    at full speed again.
    """
    entry = _entry(hass)
    first = _coordinator(hass, entry, _controller(rounds=STREAK))

    present, scanner = _patched_bluetooth()
    with present, scanner:
        await first.async_refresh()

    second = _coordinator(hass, entry, _controller(rounds=STREAK))

    assert second.update_interval == timedelta(seconds=TIMEOUT_BACKOFF_INTERVAL_S)
