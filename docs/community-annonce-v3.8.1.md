# community.home-assistant.io announcement — v3.8.0 / v3.8.1

> Post in the existing integration thread:
> https://community.home-assistant.io/t/control-daikin-madoka-brc1h-thermostat-via-bluetooth-ha-custom-integration-esphome-component/984675

---

## Daikin Madoka (BRC1H) v3.8.1 — a thermostat can no longer become unrecoverable

Hi all,

[v3.8.1](https://github.com/dasimon135/daikin_madoka/releases/tag/v3.8.1) is out, and it is the largest reliability release so far: the whole **connection / pairing / recovery layer** has been rewritten after a full review of it. Three real failures on my own 4-thermostat installation drove the work, and everything below was validated on that hardware.

### What went wrong

One thermostat, whose Bluetooth bond was perfectly fine, was accused of having **refused the pairing** and locked out. Two others ended up in `setup_retry` — a state where **every entity disappears**, including the **Reconnect** button the integration's own notification tells you to press. And neither could be re-paired by any documented route: the 60-second "walk to the thermostat" budget was nested inside a 30-second connect budget and was silently cancelled at ~28 s.

Three separate bugs, one shared root cause: the code had never written down the rule that governs all of this.

> A bond is stored **per proxy**. On a path that already holds a bond, nobody has to touch the thermostat — so a pairing **timeout** there means congestion, never a lost bond. Only an explicit **refusal** proves a human is needed.

Without that rule stated, the integration had accumulated four overlapping safety windows, each added after a previous incident, and none of them was the rule itself.

### What changed

- **A timeout is no longer treated as a refusal.** Only an explicit refusal quarantines a thermostat. A run of timeouts instead slows retries right down and raises a plain warning saying pairing is not completing — without accusing the thermostat of anything.
- **Pairing budgets are sized against the number of proxies that will be tried**, so a verdict can actually form. Previously, with two or more proxies in range, no verdict could ever be reached at all.
- **A configured thermostat now always loads**, degraded when it cannot connect, instead of vanishing into `setup_retry`. Its entities exist and read `unavailable` — the **Reconnect** button among them.
- **A re-pairing flow** appears on the integration entry after a genuine refusal, with a **Fix** button, and works even when no entity is available. It reports success or failure instead of leaving you guessing.
- **New `connection_status` sensor**: `connected`, `retrying`, `pairing not completing`, `pairing required`, `not advertising`. It stays available when the link is down — as do the signal-strength and connection-source sensors, which read Home Assistant's own data and never needed the thermostat.
- **Proxies with no free connection slot are tried last**, so a saturated proxy stops blocking a thermostat while others sit idle.
- **Your actions now take effect immediately** (v3.8.1): pressing Reconnect or submitting the re-pairing flow cancels the slow-retry backoff and retries at once, instead of leaving your trip to the thermostat with no visible effect for fifteen minutes.
- Bonds are recorded as soon as pairing succeeds, a proxy that keeps refusing is dropped (never the last one), and renaming a thermostat no longer wipes its list of bonded proxies.
- Requires **pymadoka-ng 0.3.10** (installed automatically), which now reports *why* pairing failed instead of leaving the integration to guess.
- **Madoka Card 0.7.1**: the card no longer reports a successful reconnect that never happened — it shows a real pending state and a visible error if the call fails. Hard-refresh your browser once.

Test coverage went from 102 to 223, and the ESPHome component is now **actually compiled in CI** on every change (it never had been).

### The gotcha that cost me a day — worth knowing

A pairing responder is declared **per (proxy, thermostat) pair**. Each of my four proxies only listed the two thermostats it was originally built for — but all four are *active* and in range of all four thermostats, so Home Assistant happily routed a pairing attempt through a proxy that had **no responder for that address**. Nothing answers the numeric comparison, the attempt can only time out, and **no amount of confirming at the thermostat helps**.

If you run several active proxies: **every active proxy needs a responder for every thermostat it can reach** (or set it to `bluetooth_proxy: active: false`). Also size `esp32_ble: max_connections` as `3 + number of responders` — too low and the responders fail *silently* at boot, reproducing the exact same symptom. Full configuration in the [ESPHome proxy reference](https://github.com/dasimon135/daikin_madoka/blob/main/docs/esphome-proxy.md).

### Breaking change — ESPHome component only

The dual setpoint was hardcoded, so the ESP32 climate entity always exposed two temperatures regardless of the thermostat's range setting. It is now the `dual_setpoint:` option, **defaulting to a single setpoint**. If you rely on the dual UI — or call `climate.set_temperature` with `target_temp_low`/`target_temp_high` on an ESPHome Madoka entity — add `dual_setpoint: true` to the climate block and recompile. **The Home Assistant integration is unaffected**; it already switched automatically.

### Upgrading

Via HACS (custom repository `dasimon135/daikin_madoka` if you haven't added it), then restart. Entity IDs and history are preserved, and there is nothing to reconfigure. The restart takes slightly longer than usual because Home Assistant installs the new library version.

Feedback welcome here or on [GitHub](https://github.com/dasimon135/daikin_madoka/issues)!
