"""Data update coordinator for Daikin Madoka thermostats."""

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from pymadoka import ConnectionException, Controller, PairingRequiredError
from pymadoka.connection import ConnectionStatus

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    AUTOMATIC_PAIR_TIMEOUT,
    BRC1H_NAME_PREFIX,
    CONF_BONDED_SOURCES,
    CONF_MAC,
    CONF_PAIRING_STATE,
    CONF_PREFERRED_SOURCE,
    CONNECT_TIMEOUT,
    DOMAIN,
    PAIRING_CONNECT_TIMEOUT,
    PAIRING_WINDOW_TIMEOUT,
    PAIRING_WINDOW_TTL_S,
    POLL_TIMEOUT,
    STALE_GRACE,
    TIMEOUT_BACKOFF_INTERVAL_S,
)

try:  # pragma: no cover - exercised only against a future pymadoka
    from pymadoka.connection import PAIRING_TIMEOUT_ROUNDS
except ImportError:  # pragma: no cover - upstream rename guard
    PAIRING_TIMEOUT_ROUNDS = 3

_LOGGER = logging.getLogger(__name__)

# Consecutive failed polls before we raise a user-facing repair issue.
UNREACHABLE_THRESHOLD = 5
# Follow-up refresh delay after a command, to catch the device applying it
# without waiting a whole poll interval.
BOOST_DELAY = 4
DOCS_URL = "https://github.com/dasimon135/daikin_madoka#requirements"

# One BLE connect at a time across every Madoka device. Several thermostats
# reconnecting at once (as they do after a restart) contend for the same
# ESPHome proxies, and the resulting delays push the pairing handshake of
# perfectly valid bonds past its timeout.
CONNECT_LOCK_KEY = f"{DOMAIN}_connect_lock"
PAIRING_STATE_KEY = f"{DOMAIN}_pairing_state"


def _async_connect_lock(hass: HomeAssistant) -> asyncio.Lock:
    """Return the connect lock shared by every Madoka coordinator."""
    return hass.data.setdefault(CONNECT_LOCK_KEY, asyncio.Lock())


def _describe(err: BaseException) -> str:
    """Render an exception for a user-visible message.

    str(TimeoutError()) is the empty string, and several BLE errors raise with
    no message at all, which produced log lines ending in a bare "…: ". The
    class name is never empty and is exactly the missing information.
    """
    return str(err) or type(err).__name__


def connection_profile(pairing_window: bool) -> tuple[float, float]:
    """Return (inner pair budget, outer connect budget) for one attempt.

    The profile follows from who initiated the attempt: an automatic reconnect
    needs no human (a bonded path re-encrypts on its own), a user-opened
    pairing window does. Both obey the invariant documented in const.py — the
    pair budget stays strictly below the connect budget, or the library can
    never classify a failure and the quarantine stops working.
    """
    if pairing_window:
        return PAIRING_WINDOW_TIMEOUT, PAIRING_CONNECT_TIMEOUT
    return AUTOMATIC_PAIR_TIMEOUT, CONNECT_TIMEOUT


@dataclass
class MadokaPairingState:
    """Per-device pairing state that must outlive a config entry retry.

    A failed setup raises ConfigEntryNotReady, and HA then re-runs
    async_setup_entry with a brand-new Controller, Connection and coordinator.
    Anything held on those objects resets on every retry, so it never
    accumulates and the device gets hammered forever — which is exactly how a
    single pairing problem turned into a storm. Keyed by MAC in hass.data,
    this survives.
    """

    last_error: PairingRequiredError | None = None
    # True while the user has deliberately asked to pair: unbonded proxies are
    # reachable and the pairing budget is widened for human confirmation.
    pairing_window: bool = False
    # Continuation of the library's all-paths-timed-out streak across
    # Connection rebuilds. The 3-round threshold behind PairingRequiredError
    # is unreachable without it: CONNECT_TIMEOUT cuts a setup attempt after
    # ~2 rounds, and the rebuilt Connection would restart the count at zero
    # (field incident 2026-07-21: an endless prompt salvo every 600s).
    timeout_rounds: int = 0
    # True once a pairing refusal has been PROVEN (a path explicitly rejected
    # the bond). Automatic reconnects then stop touching the device entirely —
    # every new attempt re-initiates SMP, which prompts a screen nobody is
    # watching and, repeated, jams the BRC1H's Bluetooth stack. Lifted only by
    # a deliberate user pairing action (the reconnect button) or a successful
    # session. A mere timeout streak must never set this: on a bonded path a
    # timeout means congestion, and convicting on it falsely locks out a
    # perfectly healthy thermostat (field incident 2026-07-26).
    suspended: bool = False
    # True while the device is in timeout backoff: pairing keeps timing out,
    # which is evidence of *something* but proof of nothing. Not a conviction —
    # the device is still probed, just far more slowly. Survives the
    # coordinator rebuild for the same reason timeout_rounds does.
    backoff: bool = False
    # Consecutive failed polls. Shared, not per-coordinator: HA builds a fresh
    # coordinator on every setup retry and each one performs exactly one
    # refresh, so an instance attribute could never reach UNREACHABLE_THRESHOLD
    # and the device_unreachable repair could never fire — two dead thermostats
    # produced zero notifications (field incident 2026-07-26).
    fail_count: int = 0

    def as_stored(self) -> dict[str, Any] | None:
        """Serialize the durable part of the verdict, or None if it is empty.

        pairing_window is deliberately absent: a window is permission for one
        deliberate attempt by a user who is standing at the thermostat, so it
        must never be restored by a restart. An all-default verdict serializes
        to None so a healthy device stores nothing at all.
        """
        stored: dict[str, Any] = {}
        if self.suspended:
            stored["suspended"] = True
        if self.backoff:
            stored["backoff"] = True
        if self.timeout_rounds:
            stored["timeout_rounds"] = self.timeout_rounds
        if self.fail_count:
            # Clamped: past the threshold a larger number changes nothing, and
            # the clamp is what stops a permanently dead device from rewriting
            # the config entry on every single poll.
            stored["fail_count"] = min(self.fail_count, UNREACHABLE_THRESHOLD)
        if self.last_error is not None:
            stored["last_error"] = {
                "tried_sources": list(self.last_error.tried_sources)
            }
        return stored or None

    @classmethod
    def from_stored(
        cls, address: str, stored: Mapping[str, Any]
    ) -> "MadokaPairingState":
        """Rebuild a verdict persisted by as_stored()."""

        def _count(key: str) -> int:
            value = stored.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else 0

        last_error = None
        raw_error = stored.get("last_error")
        if isinstance(raw_error, Mapping):
            sources = raw_error.get("tried_sources")
            last_error = PairingRequiredError(
                address, list(sources) if isinstance(sources, list) else []
            )
        return cls(
            last_error=last_error,
            timeout_rounds=_count("timeout_rounds"),
            suspended=bool(stored.get("suspended")),
            backoff=bool(stored.get("backoff")),
            fail_count=_count("fail_count"),
        )


def async_pairing_state(hass: HomeAssistant, address: str) -> MadokaPairingState:
    """Return (creating if needed) the shared pairing state for a device."""
    store: dict[str, MadokaPairingState] = hass.data.setdefault(
        PAIRING_STATE_KEY, {}
    )
    return store.setdefault(address, MadokaPairingState())


@callback
def async_restore_pairing_state(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Rehydrate the verdicts this entry persisted, once per MAC.

    Without this an HA restart forgets everything the integration concluded
    about a bond: the quarantine silently disarms itself and the whole
    diagnosis has to be re-derived from scratch, at poll cadence, while the
    thermostat is prompted again. A MAC that already has a live state is left
    alone — memory always wins, so a reload can never resurrect a quarantine
    that a successful connect cleared just before it.
    """
    stored = entry.data.get(CONF_PAIRING_STATE)
    if not isinstance(stored, Mapping):
        return
    store: dict[str, MadokaPairingState] = hass.data.setdefault(PAIRING_STATE_KEY, {})
    for address, raw in stored.items():
        if address in store or not isinstance(raw, Mapping):
            continue
        store[address] = MadokaPairingState.from_stored(address, raw)


@callback
def async_forget_pairing_state(
    hass: HomeAssistant, entry: ConfigEntry, address: str, *, persisted: bool = True
) -> None:
    """Drop a device's verdict from memory, and optionally from the entry.

    persisted=False is the unload case: the entry keeps its durable shadow (the
    next setup rehydrates it) but the live copy goes, so an entry that is
    deleted and re-added starts from a clean slate — the user's last-resort
    escape hatch out of a wrong quarantine.
    """
    store: dict[str, MadokaPairingState] = hass.data.get(PAIRING_STATE_KEY) or {}
    store.pop(address, None)
    if not persisted:
        return
    saved = entry.data.get(CONF_PAIRING_STATE)
    if not isinstance(saved, Mapping) or address not in saved:
        return
    remaining = {key: value for key, value in saved.items() if key != address}
    data = {**entry.data}
    if remaining:
        data[CONF_PAIRING_STATE] = remaining
    else:
        data.pop(CONF_PAIRING_STATE, None)
    hass.config_entries.async_update_entry(entry, data=data)

# Typed config entry: runtime_data maps each normalized MAC to its coordinator.
type MadokaConfigEntry = ConfigEntry[dict[str, "MadokaCoordinator"]]


class MadokaCoordinator(DataUpdateCoordinator[dict]):
    """Poll one BRC1H controller and share the result with all its entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        controller: Controller,
        scan_interval: int,
        friendly_name: str | None = None,
    ) -> None:
        """Initialize the coordinator."""
        self.controller = controller
        # The BLE stack overwrites controller.connection.name with the
        # advertised local name ("Daikin"), so keep the user's chosen name here.
        self._friendly_name = friendly_name
        self._issue_active = False
        self._pairing_issue_active = False
        self._pairing_slow_issue_active = False
        self._boost_unsub: CALLBACK_TYPE | None = None
        # Cancel handle of the pairing window's time-to-live timer, and the
        # poll interval to go back to once a timeout backoff ends.
        self._pairing_window_unsub: CALLBACK_TYPE | None = None
        self._normal_interval: timedelta | None = None
        # Pairing suspension and window live in hass.data keyed by MAC, so
        # they survive the Controller/coordinator being rebuilt on a config
        # entry retry. last_error is chained onto the skipped polls'
        # UpdateFailed so the stale-value grace keeps recognising them as a
        # pairing situation rather than an ordinary dropout.
        self._pairing = async_pairing_state(hass, controller.connection.address)
        # Resume the all-paths-timed-out streak on the freshly built
        # Connection (private attribute: pymadoka only exposes a read-only
        # property; async_reconnect already pokes sibling privates).
        controller.connection._pairing_timeout_rounds = self._pairing.timeout_rounds
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}-{controller.connection.address}",
            update_interval=timedelta(seconds=scan_interval),
        )
        # The pairing budget has exactly one owner (see connection_profile):
        # apply it up front so the controller never runs on whatever default
        # it happened to be built with.
        self._async_apply_pair_budget()
        if self._pairing.backoff:
            # A rebuild must not hand a stalling device the fast cadence back;
            # the state is per-MAC precisely so it outlives this object.
            self._normal_interval = timedelta(seconds=scan_interval)
            self.update_interval = timedelta(seconds=TIMEOUT_BACKOFF_INTERVAL_S)
        if self._pairing.pairing_window:
            # Same reason: a window whose TTL timer died with the previous
            # coordinator would otherwise stay open forever.
            self._async_arm_pairing_window_timer()

    async def _async_update_data(self) -> dict:
        """Poll the device, persisting whatever the attempt established."""
        try:
            return await self._async_update_data_inner()
        finally:
            # Every branch below mutates the shared verdict (failure counter,
            # suspension, backoff), and every mutation must reach the durable
            # copy — including the clearing a success performs. A persisted
            # quarantine that a later success could not erase would be worse
            # than no persistence at all.
            self._async_persist_pairing_state()

    async def _async_update_data_inner(self) -> dict:
        """Poll the device, tracking sustained failures for a repair issue."""
        try:
            data = await self._async_poll()
        except UpdateFailed as err:
            self._pairing.fail_count += 1
            if self._pairing.fail_count >= UNREACHABLE_THRESHOLD:
                self._raise_unreachable_issue()
            # Stale-value grace: a short BLE micro-drop should not punch holes
            # in graphs or flip entities unavailable, so the first STALE_GRACE
            # failures serve the last good data (counter incremented above, so
            # compare with <=: failures 1..STALE_GRACE are masked, the next one
            # propagates). The counter keeps rising, so the unreachable
            # threshold fires on its normal schedule. A pairing refusal never
            # heals on its own and must surface immediately; the raise site
            # chains PairingRequiredError as __cause__, which we inspect here
            # instead of tracking a per-cycle flag that would need careful
            # resetting at the start of every poll.
            # last_update_success gate: only mask success->transient dips. A
            # generic failure right after a surfaced one (e.g. a pairing
            # refusal) must not briefly resurrect entities on stale data
            # while an ERROR repair is on screen.
            if (
                self.last_update_success
                and self._pairing.fail_count <= STALE_GRACE
                and self.data
                and not isinstance(err.__cause__, PairingRequiredError)
            ):
                _LOGGER.debug(
                    "Poll %d/%d failed for %s, serving stale data: %s",
                    self._pairing.fail_count,
                    STALE_GRACE,
                    self.address,
                    err,
                )
                # Not a real success: leave the fail counter and any active
                # repair issues untouched.
                return self.data
            raise
        self._pairing.fail_count = 0
        self._clear_issues()
        # Full clear only here: an unload (which also runs _clear_issues via
        # async_shutdown_extras) must NOT lift the suspension, or a simple
        # entry reload would grant a dead bond a fresh prompt salvo.
        self._clear_pairing_suspension()
        self._async_restore_normal_interval()
        self._async_persist_preferred_source()
        return data

    async def _async_poll(self) -> dict:
        """Query all device features in one BLE session.

        A poll is also a recovery attempt: if the connection dropped (or the
        library aborted it after an unexpected error), try to re-establish it
        before querying, so entities recover without a manual reload.
        """
        if self.controller.connection.connection_status is not ConnectionStatus.CONNECTED:
            # Fail fast when the device is not even advertising: HA's BLE
            # tracker already knows, so don't burn 30s of connect attempts
            # (and a proxy connection slot) every poll for an absent device.
            if not bluetooth.async_address_present(
                self.hass, self.address, connectable=True
            ):
                raise UpdateFailed(f"Device {self.address} is not advertising")
            if self._pairing.suspended and not self._pairing.pairing_window:
                # Deliberately do NOT touch the device: a concluded pairing
                # refusal never heals on its own, and re-initiating SMP would
                # re-prompt the screen and keep its stack jammed. The repair
                # is re-raised so a rebuilt coordinator (entry retry, reload)
                # keeps the story on screen — and the flag it sets keeps the
                # redundant device_unreachable repair suppressed.
                if not self._pairing_issue_active and self._pairing.last_error:
                    self._raise_pairing_issue(self._pairing.last_error)
                raise UpdateFailed(
                    f"{self.address} needs pairing; automatic reconnects are "
                    "suspended until the reconnect button is pressed"
                ) from self._pairing.last_error
            try:
                await self._async_connect()
            except UpdateFailed:
                # A pairing window is permission for ONE deliberate attempt.
                # That attempt just failed, so the window closes here: leaving
                # it open would keep unbonded proxies reachable by unattended
                # polls, keep the human-sized SMP budget on them, and keep the
                # dead-bond quarantine disarmed — the exact combination that
                # turns one failed Reconnect into a pairing storm.
                self._async_close_pairing_window()
                raise

        try:
            async with asyncio.timeout(POLL_TIMEOUT):
                await self.controller.update()
        except (ConnectionAbortedError, ConnectionException) as err:
            raise UpdateFailed(
                f"Could not update {self.address}: {_describe(err)}"
            ) from err
        except TimeoutError as err:
            raise UpdateFailed(
                f"Polling {self.address} exceeded {POLL_TIMEOUT}s"
            ) from err

        # Snapshot (per-feature dict copies) so coordinator.data is not a live
        # view of controller state.
        status = {
            feature: dict(feature_status)
            for feature, feature_status in self.controller.refresh_status().items()
        }

        # pymadoka's Controller.update() swallows per-feature query timeouts, so
        # a connected-but-unresponsive device (e.g. an authenticated link that
        # never completes a GATT exchange) would otherwise look like a
        # successful poll of empty data. Treat "no feature answered" as failure
        # so the entry stays not-ready instead of exposing phantom entities.
        if not status:
            raise UpdateFailed(f"Device {self.address} did not answer any query")

        return status

    async def _async_connect(self) -> None:
        """Establish the BLE link under the profile this attempt earned.

        Everything about the attempt — which proxies are reachable, how long
        pairing may take, how long the whole connect may take — follows from
        one bit: is a user-initiated pairing window open? (The candidate
        filtering side of it lives in __init__._candidates.)
        """
        connect_budget = self._async_apply_pair_budget()
        try:
            # Serialized: a connect storm across devices is what pushes
            # valid bonds past their pairing timeout in the first place.
            async with _async_connect_lock(self.hass):
                await asyncio.wait_for(
                    self.controller.start(), timeout=connect_budget
                )
        except PairingRequiredError as err:
            self._async_note_pairing_error(err)
            raise UpdateFailed(_describe(err)) from err
        except Exception as err:
            # DeviceUnreachableError (and a wait_for TimeoutError) lands
            # here on purpose: both feed the threshold-based
            # device_unreachable repair, not a dedicated issue.
            # Persist the all-paths-timed-out streak first: this is the
            # very failure mode where the attempt got cancelled before
            # the library could reach its own 3-round conclusion. The max
            # guard keeps a round-less failure (device off, streak-blind
            # future library) from erasing an accumulated streak.
            self._note_timeout_rounds()
            raise UpdateFailed(
                f"Could not reconnect to {self.address}: {_describe(err)}"
            ) from err
        if (
            self.controller.connection.connection_status
            is not ConnectionStatus.CONNECTED
        ):
            raise UpdateFailed(f"Device {self.address} is not reachable")

    async def async_boost(self) -> None:
        """Refresh now and once more shortly after.

        Called after a write command so the UI reflects the device applying it
        (or a stale value snapping back) without waiting a whole poll interval.
        """
        await self.async_request_refresh()
        if self._boost_unsub is not None:
            self._boost_unsub()

        @callback
        def _followup(_now) -> None:
            self._boost_unsub = None
            self.hass.async_create_task(self.async_request_refresh())

        self._boost_unsub = async_call_later(self.hass, BOOST_DELAY, _followup)

    async def async_reconnect(self) -> None:
        """Force a fresh BLE connection, then refresh."""
        conn = self.controller.connection
        try:
            await self.controller.stop()
        except Exception:
            _LOGGER.debug("Reconnect: stop failed for %s", self.address, exc_info=True)
        # stop() -> cleanup() sets the library's _closing flag, which makes the
        # next start() bail out immediately; clear it so the coordinator's poll
        # can actually re-establish the link. Also clear _paired so the fresh
        # connection re-authenticates (the bond is per proxy).
        conn._closing = False
        conn._paired = False
        conn.connection_status = ConnectionStatus.DISCONNECTED
        # Pressing reconnect means the user is at the thermostat and ready to
        # accept a pairing code: lift the suspension, reach proxies that hold
        # no bond yet, and widen the pairing budget so a human can actually
        # compare codes and confirm.
        self._clear_pairing_suspension()
        self._async_persist_pairing_state()
        self._async_open_pairing_window()
        # The BRC1H stops advertising while connected and takes a moment to
        # resume after a disconnect; refreshing instantly would fail fast with
        # "not advertising" and defer the reconnect to the next poll.
        await asyncio.sleep(3)
        await self.async_request_refresh()

    @callback
    def _async_persist_pairing_state(self) -> None:
        """Mirror the shared verdict into the config entry.

        hass.data keeps the verdict across a coordinator rebuild; only the
        entry keeps it across an HA restart. Written on every change, so the
        stored copy can never be more pessimistic than reality.
        """
        entry = self.config_entry
        # A coordinator built outside a real entry (or against one that was
        # removed mid-poll) has nothing to write to.
        if entry is None or self.hass.config_entries.async_get_entry(
            entry.entry_id
        ) is None:
            return
        saved = entry.data.get(CONF_PAIRING_STATE)
        saved = dict(saved) if isinstance(saved, Mapping) else {}
        updated = dict(saved)
        current = self._pairing.as_stored()
        if current is None:
            updated.pop(self.address, None)
        else:
            updated[self.address] = current
        if updated == saved:
            return
        data = {**entry.data}
        if updated:
            data[CONF_PAIRING_STATE] = updated
        else:
            data.pop(CONF_PAIRING_STATE, None)
        self.hass.config_entries.async_update_entry(entry, data=data)

    @callback
    def _async_persist_preferred_source(self) -> None:
        """Remember which proxy carried the successful session.

        The stored source primes build_candidates on the next (re)connect and
        survives restarts, so the connection returns to the bonded proxy
        instead of whichever proxy wins on RSSI. It is also appended to the
        bonded-source list: a completed authenticated session is the only
        proof that this proxy holds a bond, and automatic reconnects are
        restricted to that list so they never start a new pairing.

        Skipped for legacy multi-MAC entries (no CONF_MAC): they share one
        entry, and a single preferred_source cannot be right for several
        thermostats. None means local adapter / unknown backend — nothing
        worth pinning.
        """
        source = self.controller.connection.connected_source
        if (
            not source
            or self.config_entry is None
            or CONF_MAC not in self.config_entry.data
        ):
            return
        bonded = list(self.config_entry.data.get(CONF_BONDED_SOURCES, []))
        if source not in bonded:
            bonded.append(source)
        if (
            self.config_entry.data.get(CONF_PREFERRED_SOURCE) == source
            and self.config_entry.data.get(CONF_BONDED_SOURCES) == bonded
        ):
            return
        # async_update_entry fires the entry's update listener; ours only
        # re-applies the poll interval from options, so no side effects here.
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={
                **self.config_entry.data,
                CONF_PREFERRED_SOURCE: source,
                CONF_BONDED_SOURCES: bonded,
            },
        )

    @callback
    def _raise_unreachable_issue(self) -> None:
        # A sustained pairing refusal also trips the failure threshold; the
        # unreachable advice (power/range) would be wrong next to the
        # pairing_required ERROR (or the pairing_slow WARNING, which tells the
        # opposite story), so keep that single repair on screen.
        if self._pairing_issue_active or self._pairing_slow_issue_active:
            return
        if self._issue_active:
            return
        self._issue_active = True
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"unreachable_{self.address}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="device_unreachable",
            translation_placeholders={"device": self.device_name},
            learn_more_url=DOCS_URL,
        )

    @callback
    def _async_apply_pair_budget(self) -> float:
        """Set the pairing budget for the active profile; return the outer one.

        THE single writer of connection.pair_timeout. Every other path (open a
        window, close a window, connect) goes through here, so the budget can
        never be left at a value some earlier code path happened to set — the
        leak that kept a 60s human budget armed on unattended reconnects.
        """
        pair_budget, connect_budget = connection_profile(self._pairing.pairing_window)
        self.controller.connection.pair_timeout = pair_budget
        return connect_budget

    @callback
    def _async_open_pairing_window(self) -> None:
        """Grant one user-initiated pairing attempt, with a deadline."""
        self._pairing.pairing_window = True
        self._async_apply_pair_budget()
        self._async_arm_pairing_window_timer()

    @callback
    def _async_arm_pairing_window_timer(self) -> None:
        """(Re)start the window's time-to-live."""
        self._async_cancel_pairing_window_timer()

        @callback
        def _expire(_now) -> None:
            self._pairing_window_unsub = None
            if self._pairing.pairing_window:
                _LOGGER.debug(
                    "Pairing window for %s expired after %ss",
                    self.address,
                    PAIRING_WINDOW_TTL_S,
                )
            self._async_close_pairing_window()

        self._pairing_window_unsub = async_call_later(
            self.hass, PAIRING_WINDOW_TTL_S, _expire
        )

    @callback
    def _async_cancel_pairing_window_timer(self) -> None:
        if self._pairing_window_unsub is not None:
            self._pairing_window_unsub()
            self._pairing_window_unsub = None

    @callback
    def _async_close_pairing_window(self) -> None:
        """Close the window and hand the pairing budget back to AUTOMATIC.

        Idempotent, and the only close path: whether the attempt succeeded,
        failed or was never made, an open window lifts the bonded-proxy
        restriction and disarms the quarantine, so it must always end.
        """
        self._async_cancel_pairing_window_timer()
        if not self._pairing.pairing_window:
            return
        self._pairing.pairing_window = False
        self._async_apply_pair_budget()

    @callback
    def _note_timeout_rounds(self) -> None:
        """Carry the library's all-paths-timed-out streak across rebuilds."""
        rounds = getattr(self.controller.connection, "pairing_timeout_rounds", None)
        if isinstance(rounds, int) and not isinstance(rounds, bool):
            self._pairing.timeout_rounds = max(self._pairing.timeout_rounds, rounds)

    @callback
    def _async_note_pairing_error(self, err: PairingRequiredError) -> None:
        """Decide what a PairingRequiredError actually proves, and react.

        pymadoka raises the same exception, with the same message and
        attributes, for two epistemically different events: a path that
        explicitly REJECTED the authenticated bond (a human must re-pair) and
        every path merely TIMING OUT for PAIRING_TIMEOUT_ROUNDS rounds (an
        inference congestion alone can produce). The only side channel is the
        public round counter: the rejection raise resets it to 0, the streak
        raise leaves it at the threshold. It is undocumented and fragile, so
        anything we cannot read as an integer takes the non-convicting branch
        — a wrong quarantine needs a human at the thermostat to undo, a wrong
        backoff only costs time. The robust fix is upstream: a `reason`
        attribute on PairingRequiredError (see docs/plans, section 7).
        """
        rounds = getattr(self.controller.connection, "pairing_timeout_rounds", None)
        proven_rejection = (
            isinstance(rounds, int) and not isinstance(rounds, bool) and rounds == 0
        )
        if proven_rejection:
            self._note_pairing_failure(err)
            self._raise_pairing_issue(err)
            self._async_start_reauth()
            return
        _LOGGER.warning(
            "%s did not complete pairing after %s timed-out rounds; slowing "
            "reconnects to %ss instead of concluding it refused the bond",
            self.address,
            rounds if isinstance(rounds, int) else PAIRING_TIMEOUT_ROUNDS,
            TIMEOUT_BACKOFF_INTERVAL_S,
        )
        self._note_timeout_rounds()
        self._async_enter_timeout_backoff(err)

    @callback
    def _async_start_reauth(self) -> None:
        """Ask HA for the recovery affordance that needs no entities at all.

        Deliberately NOT by raising ConfigEntryAuthFailed. The first refresh
        runs under raise_on_auth_failed=True, so that exception escapes
        async_setup_entry and puts the entry in SETUP_ERROR — no entities,
        exactly the catch-22 the unconditional degraded load just removed, and
        precisely on the post-restart poll where a rejection surfaces. It would
        also stop the coordinator rescheduling itself (_async_refresh skips
        _schedule_refresh when auth_failed), so the device could never recover
        on its own once a single rejection was recorded.

        async_start_reauth keeps the entry loaded and polling, is idempotent
        (HA drops the call while a reauth or reconfigure flow is already open
        for this entry), and adds a repair with a Fix button ALONGSIDE the
        Reconnect button rather than instead of it — two independent ways out,
        one of which survives having no dashboard and no entities.
        """
        if self.config_entry is None:
            return
        self.config_entry.async_start_reauth(self.hass)

    @callback
    def _note_pairing_failure(self, err: PairingRequiredError) -> None:
        """Suspend automatic reconnects: each retry would re-prompt the screen."""
        self._pairing.last_error = err
        self._pairing.suspended = True
        self._pairing.backoff = False
        # The accusation has been delivered; the streak starts over after the
        # user re-pairs.
        self._pairing.timeout_rounds = 0

    @callback
    def _async_enter_timeout_backoff(self, err: PairingRequiredError) -> None:
        """Slow down hard, but never convict.

        A timeout streak is evidence of something and proof of nothing. On a
        bonded path it usually means congestion (and the device recovers on
        its own), but a genuinely dead BRC1H bond also fails by silent
        timeout — so attempts must neither stop (the device would never come
        back without a human) nor continue at poll cadence (that is the SMP
        prompt storm). One attempt every TIMEOUT_BACKOFF_INTERVAL_S, plus a
        repair that suggests re-pairing without asserting a refusal.
        """
        self._pairing.last_error = err
        self._pairing.backoff = True
        self._raise_pairing_slow_issue(err)
        backoff = timedelta(seconds=TIMEOUT_BACKOFF_INTERVAL_S)
        if self.update_interval != backoff:
            self._normal_interval = self.update_interval
            self.update_interval = backoff

    @callback
    def _async_restore_normal_interval(self) -> None:
        """Back to the configured cadence after a successful poll."""
        self._pairing.backoff = False
        if self._normal_interval is None:
            return
        # If the user changed the poll interval meanwhile, the options update
        # listener already wrote it: leave their value alone.
        if self.update_interval == timedelta(seconds=TIMEOUT_BACKOFF_INTERVAL_S):
            self.update_interval = self._normal_interval
        self._normal_interval = None

    @callback
    def _clear_pairing_suspension(self) -> None:
        """Allow the next poll to connect immediately."""
        self._pairing.suspended = False
        self._pairing.timeout_rounds = 0
        self._pairing.last_error = None

    @callback
    def _raise_pairing_issue(self, err: PairingRequiredError) -> None:
        """Raise an immediate repair: an auth refusal never heals on its own."""
        # No idempotence guard: async_create_issue updates in place, and
        # re-creating keeps the {proxies} placeholder current when a later
        # refusal went through a different proxy set. The flag only feeds
        # the unreachable-repair suppression and _clear_issues.
        self._pairing_issue_active = True
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"pairing_required_{self.address}",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="pairing_required",
            translation_placeholders={
                "device": self.device_name,
                "proxies": self._proxy_names(err.tried_sources),
            },
            learn_more_url=DOCS_URL,
        )

    @callback
    def _raise_pairing_slow_issue(self, err: PairingRequiredError) -> None:
        """Warn that pairing never completes — without accusing the device.

        Deliberately WARNING, not ERROR, and worded as a suggestion: the only
        thing established here is that the handshake keeps timing out, which a
        congested proxy explains just as well as a lost bond.
        """
        self._pairing_slow_issue_active = True
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"pairing_slow_{self.address}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="pairing_slow",
            translation_placeholders={
                "device": self.device_name,
                "proxies": self._proxy_names(err.tried_sources),
            },
            learn_more_url=DOCS_URL,
        )

    def _proxy_names(self, sources: list[str | None]) -> str:
        """Resolve proxy source MACs to human-readable scanner names."""
        names = []
        for source in sources:
            if source is None:
                names.append("local adapter")
                continue
            scanner = bluetooth.async_scanner_by_source(self.hass, source)
            names.append(getattr(scanner, "name", None) or source)
        return ", ".join(dict.fromkeys(names)) or "unknown"

    @callback
    def _clear_issues(self) -> None:
        # Always delete (idempotent): an issue persisted across a restart must
        # be cleared on the first success even though the flags are False on
        # the fresh coordinator instance.
        self._issue_active = False
        self._pairing_issue_active = False
        self._pairing_slow_issue_active = False
        # The window is permission for one deliberate attempt, not a standing
        # grant: close it as soon as the device is reachable again. The
        # suspension is deliberately NOT cleared here — this also runs on
        # unload, and a reload must not re-arm the prompt salvo.
        self._async_close_pairing_window()
        ir.async_delete_issue(self.hass, DOMAIN, f"unreachable_{self.address}")
        ir.async_delete_issue(self.hass, DOMAIN, f"pairing_required_{self.address}")
        ir.async_delete_issue(self.hass, DOMAIN, f"pairing_slow_{self.address}")

    @callback
    def async_shutdown_extras(self) -> None:
        """Cancel pending timers and clear the repair issues on unload."""
        if self._boost_unsub is not None:
            self._boost_unsub()
            self._boost_unsub = None
        # Explicit even though _clear_issues closes the window too: an armed
        # TTL callback must never outlive the coordinator it points at.
        self._async_cancel_pairing_window_timer()
        self._clear_issues()

    @property
    def fail_count(self) -> int:
        """Consecutive failed polls (reset to 0 by a successful poll)."""
        return self._pairing.fail_count

    @property
    def pairing_suspended(self) -> bool:
        """True while automatic reconnects are suspended pending a re-pair."""
        return self._pairing.suspended

    @property
    def unreachable_issue_active(self) -> bool:
        """True while this coordinator has a device_unreachable repair open."""
        return self._issue_active

    @property
    def pairing_issue_active(self) -> bool:
        """True while this coordinator has a pairing_required repair open."""
        return self._pairing_issue_active

    @property
    def pairing_slow_issue_active(self) -> bool:
        """True while this coordinator has a pairing_slow repair open."""
        return self._pairing_slow_issue_active

    @property
    def pairing_backoff(self) -> bool:
        """True while pairing timeouts have slowed this device's polling."""
        return self._pairing.backoff

    @property
    def address(self) -> str:
        """Return the MAC address of the device."""
        return self.controller.connection.address

    @property
    def device_name(self) -> str:
        """Return the display name of the device."""
        return self._friendly_name or self.controller.connection.name or self.address

    @property
    def device_info(self) -> DeviceInfo:
        """Return shared device registry information."""
        info = self.controller.info or {}
        model = info.get("Model Number String")
        return DeviceInfo(
            identifiers={(DOMAIN, self.address)},
            name=self.device_name,
            manufacturer="DAIKIN",
            model=f"{BRC1H_NAME_PREFIX}{model}" if model else BRC1H_NAME_PREFIX,
            sw_version=info.get("Software Revision String"),
            hw_version=info.get("Hardware Revision String"),
        )
