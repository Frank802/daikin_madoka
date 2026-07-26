"""Daikin Madoka consts."""

DOMAIN = "daikin_madoka"
CONF_MAC = "address"
CONF_FRIENDLY_NAME = "friendly_name"
# Source (proxy) MAC of the path that last authenticated successfully; the
# candidates list is ordered sticky-first so reconnects go back to the bonded
# proxy instead of whichever proxy wins on RSSI.
CONF_PREFERRED_SOURCE = "preferred_source"
# Every proxy that has completed an authenticated session with this device, so
# it is known to hold a bond. Automatic reconnects are restricted to these:
# connecting through an unbonded proxy starts a real numeric-comparison
# pairing, which no unattended retry can ever complete and which jams the
# BRC1H when repeated. Empty/absent means "not known yet" — treated as
# unrestricted so a fresh install can still find its first path.
CONF_BONDED_SOURCES = "bonded_sources"

BRC1H_NAME_PREFIX = "BRC1H"

# Advertised by the BRC1H (local_name is just "Daikin", so the service UUID is
# the reliable discovery signal). Must stay lowercase for HA matchers.
MADOKA_SERVICE_UUID = "2141e110-213a-11e6-b67b-9e71128cae77"

MIN_TEMP = 16
MAX_TEMP = 32

DEFAULT_SCAN_INTERVAL = 60
# Failed polls masked by serving the last good data instead of raising: a
# one-off BLE micro-drop should not punch holes in graphs or flip entities
# unavailable. Kept well below UNREACHABLE_THRESHOLD so a real outage still
# surfaces quickly; pairing refusals are never masked.
STALE_GRACE = 2
# ---------------------------------------------------------------------------
# Connection budgets — THE INVARIANT: the inner pair budget must always stay
# STRICTLY BELOW the outer connect budget that wraps controller.start().
#
# Not a matter of taste. pymadoka wraps its own pair() in wait_for(pair_timeout)
# and only counts a pairing round when that inner timeout fires. If pair_timeout
# is greater than or equal to the outer budget, the whole attempt is cancelled
# first, no round is ever counted, no verdict (rejection vs timeout streak) can
# ever form — and the dead-bond quarantine silently stops working. v3.7.1
# shipped BOOT_PAIR_TIMEOUT = 30 under CONNECT_TIMEOUT = 30 and did exactly
# that, in the post-restart window where the quarantine matters most.
#
# Two profiles, selected by who initiated the attempt (see
# coordinator.connection_profile):
#
#   profile         inner pair budget            outer connect budget
#   AUTOMATIC       AUTOMATIC_PAIR_TIMEOUT 22    CONNECT_TIMEOUT          30
#   USER_INITIATED  PAIRING_WINDOW_TIMEOUT 60    PAIRING_CONNECT_TIMEOUT  90
# ---------------------------------------------------------------------------

# Must exceed the connect path's internal budget (establish_connection retries
# + pairing + settle), or reconnects get cancelled mid-handshake.
CONNECT_TIMEOUT = 30

# AUTOMATIC profile. No human is involved, so this budget is not about giving
# anyone time to confirm: it is about letting a *valid* bond finish encrypting
# through a congested ESPHome proxy after a restart, which the tight 8s
# library default (pymadoka DEFAULT_PAIR_TIMEOUT) mistakes for a timeout round.
# Wide enough for that, still under CONNECT_TIMEOUT so pymadoka's own timeout
# always fires first and the evidence is collected.
AUTOMATIC_PAIR_TIMEOUT = 22.0

# USER_INITIATED profile: a pairing window is open because the user pressed
# Reconnect and is standing at the thermostat. Long enough to walk over,
# compare the 6-digit code and accept.
PAIRING_WINDOW_TIMEOUT = 60.0
# ...which only becomes real if the outer budget leaves room for it. Under the
# ordinary CONNECT_TIMEOUT the 60s human budget was dead config: every attempt
# was cancelled at ~28s.
PAIRING_CONNECT_TIMEOUT = 90.0

# A pairing window is a loan. It closes on the first attempt that consumes it
# (success or failure) and, failing that, on this deadline — an open window
# lifts the bonded-proxy restriction and disarms the quarantine, so it must
# never outlive the user standing at the thermostat.
PAIRING_WINDOW_TTL_S = 180.0

# Poll interval imposed on a device whose pairing attempts keep timing out
# (never on one that was explicitly rejected — that is quarantined instead).
# A timeout streak is only an inference, so the device is not convicted; but a
# dead BRC1H bond does fail by silent timeout, so attempts must not continue at
# the normal cadence either. 15 minutes keeps a real recovery reachable without
# re-creating the prompt storm.
TIMEOUT_BACKOFF_INTERVAL_S = 900.0
# Discovery adverts below this RSSI are almost certainly a neighbour's BRC1H
# bleeding through a wall: don't offer a discovery card for a device the user
# can't actually pair with. Manual setup (async_step_user) is the escape
# hatch — it never filters on signal strength.
RSSI_DISCOVERY_FLOOR = -90
# Ceiling on the config-flow validation connect. Matches CONNECT_TIMEOUT's
# rationale: it must exceed the connect path's internal budget (candidate
# retries + pairing prompt + settle) or a slow-but-successful first pairing
# gets cut short and reported as cannot_connect.
VALIDATE_TIMEOUT = 30
# Hard ceiling on one full-feature poll (queries retry individually).
POLL_TIMEOUT = 45
