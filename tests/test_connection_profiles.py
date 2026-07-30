"""Two connection profiles, one invariant: inner pair budget < outer budget.

Field regression 2026-07-25/26. v3.7.1 widened the automatic pairing budget to
BOOT_PAIR_TIMEOUT = 30s so a valid-but-slow bond could finish encrypting at
boot. It did fix that, and it broke something else: the outer wait that wraps
controller.start() is also 30s, so pymadoka's pair() was always cancelled
before its own timeout could fire. The library never counted a pairing round,
never formed a verdict, and a genuinely dead bond could no longer be
diagnosed - the anti-storm quarantine silently disarmed itself in exactly the
post-restart window it exists for.

The fix stated that invariant PER ATTEMPT, and that was the second half of the
same regression: pymadoka counts a pairing ROUND only after walking EVERY
candidate, so a round costs N * (connect overhead + pair budget). 22 < 30 holds
for one path and for no other number, and with 3-4 proxies seeing each
thermostat the round counter stayed at 0 forever - same missing verdict, same
disarmed quarantine, plus a 4.4x amplification of pairing attempts because the
timeout backoff hangs off that verdict.

Hence the invariant this file guards, in its per-round form:

    N * (CANDIDATE_CONNECT_OVERHEAD_S + pair budget) < outer connect budget

so the library's classifier can always run and evidence is always collected.

Two profiles, selected by who initiated the attempt:

- AUTOMATIC (no pairing window): wide enough for a slow bond behind a
  congested proxy, still under CONNECT_TIMEOUT.
- USER_INITIATED (pairing window open): the human-sized 60s budget, with an
  outer budget raised to match so it stops being dead config.

This file replaces test_boot_window_pair_timeout.py; the boot window and
BOOT_PAIR_TIMEOUT are gone, superseded by the AUTOMATIC profile.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from pymadoka import PairingRequiredError
from pymadoka.connection import ConnectionStatus
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.core import HomeAssistant

from custom_components.daikin_madoka.const import (
    AUTOMATIC_PAIR_TIMEOUT,
    CANDIDATE_CONNECT_OVERHEAD_S,
    CONF_MAC,
    CONNECT_TIMEOUT,
    DOMAIN,
    MIN_PAIR_TIMEOUT,
    PAIRING_CONNECT_TIMEOUT,
    PAIRING_WINDOW_TIMEOUT,
    ROUND_HEADROOM_S,
)
from custom_components.daikin_madoka.coordinator import (
    MadokaCoordinator,
    async_pairing_state,
    connection_profile,
)

MAC = "D0:CF:13:0F:11:F6"
SOURCE = "AA:BB:CC:11:22:33"
# pymadoka's DEFAULT_PAIR_TIMEOUT: the tight steady-state budget a controller
# is built with, which the coordinator now always overrides with a profile.
DEFAULT_PAIR_TIMEOUT = 8.0

BLUETOOTH = "homeassistant.components.bluetooth"


def _disconnected_controller(rounds: int = 0) -> MagicMock:
    controller = MagicMock()
    controller.connection.address = MAC
    controller.connection.name = "Daikin"
    controller.connection.connection_status = ConnectionStatus.DISCONNECTED
    controller.connection.connected_source = SOURCE
    controller.connection.pair_timeout = DEFAULT_PAIR_TIMEOUT
    controller.connection.pairing_timeout_rounds = rounds
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


def _budget_spy():
    """Record every outer connect budget without changing the behaviour."""
    seen: list[float | None] = []
    real_wait_for = asyncio.wait_for

    async def _wait_for(coro, timeout=None):
        seen.append(timeout)
        return await real_wait_for(coro, timeout)

    return seen, patch(
        "custom_components.daikin_madoka.coordinator.asyncio.wait_for", _wait_for
    )


# --------------------------------------------------------------------------
# 1. The invariant itself
# --------------------------------------------------------------------------


def test_inner_pair_budget_is_strictly_below_the_outer_connect_budget() -> None:
    """The regression that made pymadoka's verdict unreachable must not return."""
    for pairing_window in (False, True):
        pair_budget, connect_budget = connection_profile(pairing_window)
        assert pair_budget < connect_budget, (
            f"profile pairing_window={pairing_window} cancels pair() before the "
            "library can classify the failure"
        )


def test_a_full_candidate_round_fits_inside_the_outer_budget() -> None:
    """The invariant is PER ROUND, not per attempt. This is the real arithmetic.

    pymadoka increments _pairing_timeout_rounds only after walking EVERY
    candidate (every_path_failed_auth compares against len(candidates)), so a
    round costs N * (connect overhead + pair budget). Stating the invariant per
    attempt (22 < 30) held only for N == 1: with the 3-4 proxies that see each
    thermostat here, every attempt was cancelled by the outer wait with the
    round counter still at 0 — no verdict, ever, hence no backoff, hence a dead
    bond re-initiating SMP every 60s forever.

    Asserted against the numbers, not against a MagicMock with the round
    counter set by hand: a mock can never catch this class of bug.
    """
    for pairing_window in (False, True):
        for paths in (1, 2, 3, 4, 8):
            pair_budget, connect_budget = connection_profile(pairing_window, paths)
            round_cost = paths * (CANDIDATE_CONNECT_OVERHEAD_S + pair_budget)
            assert round_cost <= connect_budget, (
                f"pairing_window={pairing_window}, {paths} candidates: a round "
                f"costs {round_cost}s but the attempt is cancelled at "
                f"{connect_budget}s, so the library can never count it"
            )
            # Not merely equal: something has to be left for building the
            # candidate list and for the classification after the loop.
            assert connect_budget - round_cost >= ROUND_HEADROOM_S


def test_the_shaped_pair_budget_never_drops_below_the_floor() -> None:
    """v3.7.1's whole point: a slow-but-valid bond must still be able to finish.

    Shrinking the pair budget to make a round fit is the easy fix and the wrong
    one past MIN_PAIR_TIMEOUT — below it a healthy bond re-encrypting through a
    congested proxy starts failing again. The outer budget gives way instead.
    """
    for pairing_window in (False, True):
        for paths in (1, 2, 3, 4, 8):
            pair_budget, _ = connection_profile(pairing_window, paths)
            assert pair_budget >= MIN_PAIR_TIMEOUT


def test_more_candidates_never_shorten_the_time_available_per_path() -> None:
    """Adding a proxy must not make each path's pairing budget unusable."""
    previous = None
    for paths in (1, 2, 3, 4, 8):
        pair_budget, connect_budget = connection_profile(False, paths)
        assert connect_budget >= CONNECT_TIMEOUT
        if previous is not None:
            assert pair_budget <= previous
        previous = pair_budget


def test_a_single_candidate_still_gets_the_documented_ceilings() -> None:
    """Shaping must not quietly change the one case that already worked."""
    assert connection_profile(False, 1) == (AUTOMATIC_PAIR_TIMEOUT, CONNECT_TIMEOUT)
    assert connection_profile(True, 1) == (
        PAIRING_WINDOW_TIMEOUT,
        PAIRING_CONNECT_TIMEOUT,
    )


def test_no_candidate_at_all_behaves_like_one() -> None:
    """An empty list raises DeviceUnreachableError before any budget matters."""
    assert connection_profile(False, 0) == connection_profile(False, 1)


async def test_the_connect_budget_is_sized_from_the_live_candidate_list(
    hass: HomeAssistant,
) -> None:
    """The count comes from the callback pymadoka itself will call.

    Not from a second copy of the filtering rules: the bonded-source
    restriction, the pairing window and the free-slot ordering all change how
    many paths a connect actually walks, and a count that drifts from them puts
    the round back over the budget.
    """
    entry = _entry(hass)
    controller = _disconnected_controller()
    controller.connection.candidates_callback = lambda: [object()] * 3
    seen_pair_budgets: list[float] = []

    async def _connect() -> None:
        seen_pair_budgets.append(controller.connection.pair_timeout)
        controller.connection.connection_status = ConnectionStatus.CONNECTED

    controller.start = AsyncMock(side_effect=_connect)
    coordinator = _coordinator(hass, entry, controller)

    present, scanner = _patched_bluetooth()
    seen_connect_budgets, spy = _budget_spy()
    with present, scanner, spy:
        await coordinator.async_refresh()

    expected_pair, expected_connect = connection_profile(False, 3)
    assert seen_pair_budgets == [expected_pair]
    assert expected_connect in seen_connect_budgets
    assert expected_connect > CONNECT_TIMEOUT


async def test_a_broken_candidates_callback_falls_back_to_one_path(
    hass: HomeAssistant,
) -> None:
    """Sizing is a hint, never a reason to fail a connect."""
    entry = _entry(hass)
    controller = _disconnected_controller()

    def _boom():
        raise RuntimeError("registry unavailable")

    controller.connection.candidates_callback = _boom

    async def _connect() -> None:
        controller.connection.connection_status = ConnectionStatus.CONNECTED

    controller.start = AsyncMock(side_effect=_connect)
    coordinator = _coordinator(hass, entry, controller)

    present, scanner = _patched_bluetooth()
    with present, scanner:
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert controller.connection.pair_timeout == AUTOMATIC_PAIR_TIMEOUT


def test_profiles_expose_the_documented_budgets() -> None:
    assert connection_profile(False) == (AUTOMATIC_PAIR_TIMEOUT, CONNECT_TIMEOUT)
    assert connection_profile(True) == (
        PAIRING_WINDOW_TIMEOUT,
        PAIRING_CONNECT_TIMEOUT,
    )


def test_the_automatic_budget_still_survives_a_slow_bond() -> None:
    """v3.7.1 goal kept: much wider than the 8s default that misread congestion."""
    assert AUTOMATIC_PAIR_TIMEOUT > 2 * DEFAULT_PAIR_TIMEOUT


# --------------------------------------------------------------------------
# 2. The profiles are actually applied
# --------------------------------------------------------------------------


async def test_automatic_poll_uses_the_automatic_profile(
    hass: HomeAssistant,
) -> None:
    """No pairing window: the wide-but-bounded budget, under CONNECT_TIMEOUT."""
    entry = _entry(hass)
    controller = _disconnected_controller()
    seen_pair_budgets: list[float] = []

    async def _connect() -> None:
        seen_pair_budgets.append(controller.connection.pair_timeout)
        controller.connection.connection_status = ConnectionStatus.CONNECTED

    controller.start = AsyncMock(side_effect=_connect)
    coordinator = _coordinator(hass, entry, controller)

    present, scanner = _patched_bluetooth()
    seen_connect_budgets, spy = _budget_spy()
    with present, scanner, spy:
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert seen_pair_budgets == [AUTOMATIC_PAIR_TIMEOUT]
    assert CONNECT_TIMEOUT in seen_connect_budgets


async def test_user_initiated_attempt_uses_the_human_profile(
    hass: HomeAssistant,
) -> None:
    """An open pairing window gets 60s of pairing under a 90s outer budget."""
    entry = _entry(hass)
    state = async_pairing_state(hass, MAC)
    state.pairing_window = True
    controller = _disconnected_controller()
    seen_pair_budgets: list[float] = []

    async def _connect() -> None:
        seen_pair_budgets.append(controller.connection.pair_timeout)
        controller.connection.connection_status = ConnectionStatus.CONNECTED

    controller.start = AsyncMock(side_effect=_connect)
    coordinator = _coordinator(hass, entry, controller)

    present, scanner = _patched_bluetooth()
    seen_connect_budgets, spy = _budget_spy()
    with present, scanner, spy:
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert seen_pair_budgets == [PAIRING_WINDOW_TIMEOUT]
    assert PAIRING_CONNECT_TIMEOUT in seen_connect_budgets


async def test_the_profile_overrides_whatever_the_controller_was_built_with(
    hass: HomeAssistant,
) -> None:
    """One owner for pair_timeout: the library default never leaks through."""
    entry = _entry(hass)
    controller = _disconnected_controller()

    async def _connect() -> None:
        controller.connection.connection_status = ConnectionStatus.CONNECTED

    controller.start = AsyncMock(side_effect=_connect)
    coordinator = _coordinator(hass, entry, controller)

    present, scanner = _patched_bluetooth()
    with present, scanner:
        await coordinator.async_refresh()

    assert controller.connection.pair_timeout == AUTOMATIC_PAIR_TIMEOUT


# --------------------------------------------------------------------------
# 3. What the boot window was protecting still holds
# --------------------------------------------------------------------------


async def test_slow_but_valid_bond_is_not_quarantined(hass: HomeAssistant) -> None:
    """A bond needing more than the 8s default must connect, not quarantine.

    Modelled at the coordinator boundary: on the tight library default the
    library would conclude PairingRequiredError, on the automatic profile the
    same bond completes.
    """
    entry = _entry(hass)
    controller = _disconnected_controller()

    async def _connect() -> None:
        if controller.connection.pair_timeout <= DEFAULT_PAIR_TIMEOUT:
            raise PairingRequiredError(MAC, [SOURCE])
        controller.connection.connection_status = ConnectionStatus.CONNECTED

    controller.start = AsyncMock(side_effect=_connect)
    coordinator = _coordinator(hass, entry, controller)

    present, scanner = _patched_bluetooth()
    with present, scanner:
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert async_pairing_state(hass, MAC).suspended is False


async def test_genuine_auth_rejection_still_quarantines(hass: HomeAssistant) -> None:
    """The wider automatic budget must not weaken the real dead-bond path."""
    entry = _entry(hass)
    controller = _disconnected_controller(rounds=0)
    controller.start = AsyncMock(side_effect=PairingRequiredError(MAC, [SOURCE]))
    coordinator = _coordinator(hass, entry, controller)

    present, scanner = _patched_bluetooth()
    with present, scanner:
        await coordinator.async_refresh()

    assert not coordinator.last_update_success
    assert async_pairing_state(hass, MAC).suspended is True
