"""The shared connect lock must not let one device stall the others.

Field incident 2026-07-26, cross-device amplification (review section 5). One
asyncio.Lock serialises the BLE connect of EVERY Madoka coordinator, and it was
held across the whole wait_for(controller.start(), budget). pymadoka sleeps its
retry backoff (5 -> 10 -> 20 -> 40 -> 60s, plus a fixed 2s) INSIDE start(), so a
device with a dead bond parked the shared lock for a full budget per poll while
doing nothing at all: healthy devices' reconnects were delayed, the proxies got
more congested, and the extra congestion produced the pair timeouts that then
convicted the healthy devices too. Worse, waiting for the lock was itself
unbounded and this coordinator's own timer only started after acquisition, so N
stuck devices stacked N budgets serially.

The library's sleeps cannot be moved out from here, so the locked region cannot
be shortened; the WAIT is bounded instead. Waiting longer than one full attempt
of our own profile means the queue ahead of us is already longer than the work
we came to do - so the cycle is skipped and the next poll retries.

The rule that makes this safe: a skipped cycle never touched the device, so it
must prove nothing about it. No failure counted, no streak extended, no pairing
window consumed, no healthy entity flipped unavailable.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from pymadoka import ConnectionException
from pymadoka.connection import ConnectionStatus
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.core import HomeAssistant

from custom_components.daikin_madoka.const import (
    CONF_MAC,
    CONF_PAIRING_STATE,
    DOMAIN,
)
from custom_components.daikin_madoka.coordinator import (
    MAX_CONSECUTIVE_SKIPS,
    MadokaCoordinator,
    _async_connect_lock,
    async_pairing_state,
)
from custom_components.daikin_madoka.diagnostics import (
    async_get_config_entry_diagnostics,
)

MAC = "D0:CF:13:0F:11:F6"
SOURCE = "AA:BB:CC:11:22:33"
BLUETOOTH = "homeassistant.components.bluetooth"
COORDINATOR = "custom_components.daikin_madoka.coordinator"


def _controller() -> MagicMock:
    controller = MagicMock()
    controller.connection.address = MAC
    controller.connection.name = "Daikin"
    controller.connection.connection_status = ConnectionStatus.DISCONNECTED
    controller.connection.connected_source = SOURCE
    controller.connection.pair_timeout = 8.0
    controller.connection.pairing_timeout_rounds = 0
    controller.update = AsyncMock()
    controller.refresh_status.return_value = {"set_point": {"cooling_set_point": 25}}

    async def _start() -> None:
        controller.connection.connection_status = ConnectionStatus.CONNECTED

    controller.start = AsyncMock(side_effect=_start)
    return controller


def _coordinator(
    hass: HomeAssistant, entry: MockConfigEntry, controller: MagicMock
) -> MadokaCoordinator:
    token = config_entries.current_entry.set(entry)
    try:
        return MadokaCoordinator(hass, controller, scan_interval=60)
    finally:
        config_entries.current_entry.reset(token)


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_MAC: MAC})
    entry.add_to_hass(hass)
    return entry


def _patched_bluetooth():
    return (
        patch(f"{BLUETOOTH}.async_address_present", return_value=True),
        patch(
            f"{BLUETOOTH}.async_scanner_by_source",
            return_value=SimpleNamespace(name="Proxy"),
        ),
    )


def _tiny_budgets():
    """Shrink both profiles' outer budgets so the wait is testable in real time.

    Every part of the budget has to shrink together: connection_profile stretches
    the outer budget when the per-candidate share would fall under
    MIN_PAIR_TIMEOUT, so leaving the floor and the overheads at their real values
    would quietly restore a ~14s budget and make this a 14s test.
    """
    return (
        patch.multiple(
            COORDINATOR,
            CONNECT_TIMEOUT=0.05,
            MIN_PAIR_TIMEOUT=0.01,
            CANDIDATE_CONNECT_OVERHEAD_S=0.0,
            ROUND_HEADROOM_S=0.0,
        ),
        patch(f"{COORDINATOR}.PAIRING_CONNECT_TIMEOUT", 0.05),
    )


async def test_a_contended_lock_skips_the_cycle_instead_of_queueing(
    hass: HomeAssistant,
) -> None:
    """Waiting is what stacked N budgets serially; give up and retry later."""
    entry = _entry(hass)
    controller = _controller()
    coordinator = _coordinator(hass, entry, controller)
    lock = _async_connect_lock(hass)
    await lock.acquire()

    present, scanner = _patched_bluetooth()
    connect, pair = _tiny_budgets()
    try:
        with present, scanner, connect, pair:
            await coordinator.async_refresh()
    finally:
        lock.release()

    assert controller.start.await_count == 0


async def test_a_skipped_cycle_is_not_a_device_failure(hass: HomeAssistant) -> None:
    """Contention between our own coordinators says nothing about a thermostat."""
    entry = _entry(hass)
    controller = _controller()
    coordinator = _coordinator(hass, entry, controller)
    lock = _async_connect_lock(hass)
    await lock.acquire()

    present, scanner = _patched_bluetooth()
    connect, pair = _tiny_budgets()
    try:
        with present, scanner, connect, pair:
            await coordinator.async_refresh()
    finally:
        lock.release()

    state = async_pairing_state(hass, MAC)
    assert state.fail_count == 0
    assert state.timeout_rounds == 0
    assert state.backoff is False
    # Nothing was concluded, so nothing was written to the durable verdict.
    assert CONF_PAIRING_STATE not in entry.data


async def test_a_skipped_cycle_keeps_the_last_good_data(
    hass: HomeAssistant,
) -> None:
    """A healthy device must not blink unavailable because a sibling is stuck."""
    entry = _entry(hass)
    controller = _controller()
    coordinator = _coordinator(hass, entry, controller)

    present, scanner = _patched_bluetooth()
    with present, scanner:
        await coordinator.async_refresh()
    assert coordinator.last_update_success is True
    good = coordinator.data

    lock = _async_connect_lock(hass)
    await lock.acquire()
    controller.connection.connection_status = ConnectionStatus.DISCONNECTED
    present, scanner = _patched_bluetooth()
    connect, pair = _tiny_budgets()
    try:
        with present, scanner, connect, pair:
            await coordinator.async_refresh()
    finally:
        lock.release()

    assert coordinator.last_update_success is True
    assert coordinator.data == good


async def test_a_skipped_cycle_does_not_consume_the_pairing_window(
    hass: HomeAssistant,
) -> None:
    """The window is permission for one ATTEMPT; no attempt was made."""
    entry = _entry(hass)
    controller = _controller()
    coordinator = _coordinator(hass, entry, controller)
    state = async_pairing_state(hass, MAC)
    coordinator._async_open_pairing_window()
    assert state.pairing_window is True

    lock = _async_connect_lock(hass)
    await lock.acquire()
    present, scanner = _patched_bluetooth()
    connect, pair = _tiny_budgets()
    try:
        with present, scanner, connect, pair:
            await coordinator.async_refresh()
    finally:
        lock.release()

    assert state.pairing_window is True
    # The window's TTL timer must not outlive the test.
    coordinator.async_shutdown_extras()


async def test_stale_data_is_served_for_a_bounded_number_of_skips(
    hass: HomeAssistant,
) -> None:
    """Lock starvation must not serve hours-old readings as current, forever.

    A skip proves nothing about the device, so it counts no failure - but
    "proves nothing" became "hides everything": last_update_success stayed True
    on every skip, so entities kept showing the last good temperature as fresh
    with nothing above DEBUG and no counter anywhere. Bounded now: after
    MAX_CONSECUTIVE_SKIPS the device goes unavailable honestly.
    """
    entry = _entry(hass)
    controller = _controller()
    coordinator = _coordinator(hass, entry, controller)

    present, scanner = _patched_bluetooth()
    with present, scanner:
        await coordinator.async_refresh()
    good = coordinator.data

    lock = _async_connect_lock(hass)
    await lock.acquire()
    controller.connection.connection_status = ConnectionStatus.DISCONNECTED
    try:
        for cycle in range(1, MAX_CONSECUTIVE_SKIPS):
            present, scanner = _patched_bluetooth()
            connect, pair = _tiny_budgets()
            with present, scanner, connect, pair:
                await coordinator.async_refresh()
            assert coordinator.last_update_success is True, cycle
            assert coordinator.data == good
            assert coordinator.skipped_polls == cycle

        present, scanner = _patched_bluetooth()
        connect, pair = _tiny_budgets()
        with present, scanner, connect, pair:
            await coordinator.async_refresh()
    finally:
        lock.release()

    assert coordinator.skipped_polls == MAX_CONSECUTIVE_SKIPS
    assert coordinator.last_update_success is False
    # Still not a device failure: the streaks that drive the quarantine and the
    # repairs must stay untouched by contention between our own coordinators.
    state = async_pairing_state(hass, MAC)
    assert state.fail_count == 0
    assert state.timeout_rounds == 0
    assert coordinator.unreachable_issue_active is False


async def test_a_reached_device_resets_the_skip_counter(
    hass: HomeAssistant,
) -> None:
    """Only CONSECUTIVE skips mean starvation."""
    entry = _entry(hass)
    controller = _controller()
    coordinator = _coordinator(hass, entry, controller)

    lock = _async_connect_lock(hass)
    await lock.acquire()
    present, scanner = _patched_bluetooth()
    connect, pair = _tiny_budgets()
    try:
        with present, scanner, connect, pair:
            await coordinator.async_refresh()
    finally:
        lock.release()
    assert coordinator.skipped_polls == 1

    present, scanner = _patched_bluetooth()
    with present, scanner:
        await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert coordinator.skipped_polls == 0


async def test_the_skip_counter_is_visible_in_diagnostics(
    hass: HomeAssistant,
) -> None:
    """A counter nobody can read is not much better than no counter at all."""
    entry = _entry(hass)
    controller = _controller()
    coordinator = _coordinator(hass, entry, controller)
    entry.runtime_data = {MAC: coordinator}

    lock = _async_connect_lock(hass)
    await lock.acquire()
    present, scanner = _patched_bluetooth()
    connect, pair = _tiny_budgets()
    try:
        with present, scanner, connect, pair:
            await coordinator.async_refresh()
    finally:
        lock.release()

    _, scanner = _patched_bluetooth()
    with scanner:
        report = await async_get_config_entry_diagnostics(hass, entry)
    assert report["devices"]["device_0"]["skipped_polls"] == 1
    assert report["pairing_state"]["skipped_polls"] == 1


async def test_the_lock_is_released_when_the_connect_fails(
    hass: HomeAssistant,
) -> None:
    """A lock leaked by a failing device would block every other one for good."""
    entry = _entry(hass)
    controller = _controller()
    controller.start = AsyncMock(side_effect=ConnectionException("device off"))
    coordinator = _coordinator(hass, entry, controller)

    present, scanner = _patched_bluetooth()
    with present, scanner:
        await coordinator.async_refresh()

    assert _async_connect_lock(hass).locked() is False


async def test_the_lock_is_released_when_the_connect_times_out(
    hass: HomeAssistant,
) -> None:
    """The stuck-device case: the budget expires, the lock must still come back."""
    entry = _entry(hass)
    controller = _controller()

    async def _never_returns() -> None:
        await asyncio.sleep(5)

    controller.start = AsyncMock(side_effect=_never_returns)
    coordinator = _coordinator(hass, entry, controller)

    present, scanner = _patched_bluetooth()
    connect, pair = _tiny_budgets()
    with present, scanner, connect, pair:
        await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert _async_connect_lock(hass).locked() is False


async def test_the_next_poll_after_a_skip_connects_normally(
    hass: HomeAssistant,
) -> None:
    """Skipping is a deferral, not a state: nothing has to be cleared for it."""
    entry = _entry(hass)
    controller = _controller()
    coordinator = _coordinator(hass, entry, controller)
    lock = _async_connect_lock(hass)
    await lock.acquire()

    present, scanner = _patched_bluetooth()
    connect, pair = _tiny_budgets()
    try:
        with present, scanner, connect, pair:
            await coordinator.async_refresh()
    finally:
        lock.release()

    present, scanner = _patched_bluetooth()
    with present, scanner:
        await coordinator.async_refresh()

    assert controller.start.await_count == 1
    assert coordinator.last_update_success is True
