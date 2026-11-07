# tesserae-device-pi

Raspberry Pi-side daemon that talks to a [Tesserae](https://github.com/dmellok/tesserae)
server — either by **polling the REST API** (default for fresh installs) or by
**subscribing to an MQTT broker** — downloads PNG frames, applies the server's
rotate / scale / bg / saturation hints, and paints them onto a Pimoroni
e-ink panel via the official [`inky`](https://github.com/pimoroni/inky)
library.

This is the **PNG path** counterpart to
[`tesserae-device-pi-bin`](https://github.com/dmellok/tesserae-device-pi-bin).
Most installs only need ONE of the two.

| | tesserae-pi-bin-client | tesserae-pi-client (this) |
|---|---|---|
| Wire format | 4-bpp packed `.bin` | RGB `.png` |
| Quantise / dither done by | Tesserae server | inky lib, on the Pi |
| Hardware | Inky Impression (Spectra 6 / Waveshare E6) | any inky-supported panel |
| Speed per frame | fast (no PIL roundtrip) | slower (per-frame quantise) |
| Inky version pin | exact | range (`>=2.0,<3`) |

Pick the **png** client if any of the following is true:
- you have an Inky pHAT, Inky wHAT, or any Inky Impression
- you're migrating from an inky-dash v3/v4 setup
- you want the broadest possible hardware support

---

## Install

### One-shot (recommended)

```bash
git clone https://github.com/dmellok/tesserae-device-pi.git
cd tesserae-device-pi
./scripts/install.sh
```

Run as your normal user (NOT root, NOT via sudo). The script invokes sudo
internally for the privileged bits and runs pip in a venv owned by you.
It does, idempotently:

1. `apt-get install` the build prereqs (`python3-dev`, `build-essential`,
   `libopenjp2-7`, `libtiff6`)
2. `raspi-config nonint do_spi 0` to enable SPI
3. `usermod -aG gpio,spi,i2c $USER` for HAT access (i2c is needed for the
   HAT EEPROM read on Inky Impression; needs a re-login to take effect)
4. Create `.venv` in the repo dir
5. `pip install -e .` — pulls `inky[rpi]`, `paho-mqtt`, `Pillow`
6. **Prompts** for transport (`rest` default — just the Tesserae server URL
   — or `mqtt` for an existing broker setup), device id, the
   transport-specific fields, and whether to arm the front buttons
   (off by default), then writes
   `~/.config/tesserae-pi-client/config.toml` (skipped if it exists,
   unless `--reconfigure`)
7. Symlink `.venv/bin/tesserae-pi-client` to `/usr/local/bin/`
8. Install + enable + start the systemd service (skip with `--no-service`)

Useful flags:

```
--no-service       skip systemd unit install
--paint-test       run --paint-test after install
--skip-apt         skip apt-get
--non-interactive  never prompt — write default config if none exists
--reconfigure      overwrite existing config
--bookworm         also pip install rpi-lgpio (Pi 5 / Bookworm needs it
                   because RPi.GPIO does not work on those boards)
--user USER        user the systemd unit runs as (default: $USER)
```

### Manual (if you prefer step-by-step)

```bash
sudo apt update
sudo apt install -y python3-pip python3-dev build-essential \
                    libopenjp2-7 libtiff6
sudo raspi-config nonint do_spi 0          # pixel data to the panel
sudo raspi-config nonint do_i2c 0          # HAT EEPROM read by auto-detect
# free the CS pin so inky can drive chip-select in software:
echo 'dtoverlay=spi0-0cs' | sudo tee -a /boot/firmware/config.txt
sudo usermod -aG gpio,spi,i2c "$USER"      # i2c for HAT EEPROM read; log out + back in
# reboot so SPI + I2C + the overlay take effect before first run

git clone https://github.com/dmellok/tesserae-device-pi.git
cd tesserae-device-pi
python3 -m venv .venv
.venv/bin/pip install -e .                 # pulls inky[rpi]
# Pi 5 / Bookworm only:
.venv/bin/pip install rpi-lgpio
```

### Verify hardware

Before wiring up MQTT, paint a colour-stripe test pattern to confirm
the SPI path and panel orientation:

```bash
tesserae-pi-client --paint-test
```

If you see a refresh and a coloured stripe pattern, the hardware path is good.
If you get `could not auto-detect inky panel`, the README troubleshooting
section below is for you.

### Configure

The first run writes `~/.config/tesserae-pi-client/config.toml` with
sensible defaults. For REST mode (the new default) point `rest.server_url`
at the Tesserae server; for MQTT mode point `mqtt.host` at your broker:

```toml
transport_mode = "rest"  # mqtt | rest

[mqtt]
host = "192.168.1.10"
port = 1883
username = ""
password = ""
client_id = "pi-impression-png-1"
device_id = "pi_png"  # MQTT topic prefix (also identifies this device to REST)
keepalive = 60

[rest]
server_url = "http://tesserae.local:8765"
device_token = ""         # auto-populated after pair/discover
pairing_code = ""         # single-use; wiped after first successful register
last_frame_etag = ""      # auto-populated for If-None-Match short-circuit
poll_interval_s = 60      # fallback wake interval if server omits next_poll_s
tz = ""                   # auto-populated from the server's config.tz
sse_enabled = true        # hold GET /stream for instant repaints

[relay]
relay_url = "https://relay.tesserae.ink"
pairing_code = ""    # single-use; from Settings -> Cloud relay
# install_id / device_id / device_token / frame_key / private_key /
# last_frame_etag / config_etag are written by the pairing flow.
poll_interval_s = 60

[buttons]
enabled = false      # front buttons off by default
chip = ""            # gpiochip name; empty = autodetect
pin_a = 5
pin_b = 6
pin_c = 0            # 0 = derive from the detected panel (16 or 25)
pin_d = 24
action_a = "left"    # refresh | left | right, or "" to unmap
action_b = "refresh"
action_c = "right"
action_d = ""
debounce_ms = 50

[cache]
enabled = true       # deck pages + album frames cached locally
root = ""            # empty = ~/.cache/tesserae-pi-client

[ota]
report_enabled = true

[environment]
sensor = ""          # "" | sht4x | shtc3
i2c_bus = 1

[battery]
# Optional PiSugar UPS. pisugar3 reads the gauge over I2C directly;
# pisugar-server asks PiSugar's power manager on 127.0.0.1:8423.
source = ""          # "" | pisugar3 | pisugar-server
i2c_bus = 1

[http]
download_timeout_s = 30
max_frame_bytes = 16_000_000

[logging]
level = "INFO"
```

`device_id` identifies this Pi to Tesserae and (in MQTT mode) sets the
topic prefix `tesserae/<device_id>/...`. The default `pi_png` matches the
server's built-in `pi_png_client` kind. Give each Pi its own id
(`pi_png_lounge`, `pi_png_kitchen`, …) if you run more than one. It must
be lowercase, 2–32 chars, and start with a letter
(`^[a-z][a-z0-9_-]{1,31}$`).

Note: there is no `[panel]` section — the panel is auto-detected from the
HAT EEPROM. If detection fails the daemon refuses to start (see
troubleshooting).

> **Upgrading?** Two breakage windows to be aware of:
>
> - **Pre-`device_id` configs** (very early installs) had no `device_id`
>   key and hardcoded `tesserae/pi/...`. Those now resolve to `pi_png`
>   on parse, moving you to `tesserae/pi_png/...` — register a `pi_png`
>   device in Tesserae or set `device_id = "pi"` to keep the legacy
>   prefix.
> - **Pre-REST configs** (no `transport_mode` key) keep defaulting to
>   `transport_mode = "mqtt"` via the parser fallback, so a
>   `git pull && systemctl restart` does **not** switch you to REST.
>   Only freshly written configs (and the install prompt) default to
>   REST. To switch an existing install, add `transport_mode = "rest"`
>   at the top of `config.toml` and fill in `[rest].server_url`.

### Install as a service (if you used the manual path)

`scripts/install.sh` already does this. If you installed manually:

```bash
sudo ./scripts/install-service.sh        # uses $SUDO_USER by default
sudo journalctl -fu tesserae-pi-client
```

The unit runs as your user with `gpio` + `spi` + `i2c` group membership
(`i2c` is required so `inky.auto()` can read the HAT EEPROM at startup).

---

## Transports

The client speaks one of three transports, selected by `transport_mode`:

- **`rest`** (default for fresh installs) — polls the Tesserae server's
  `/api/v1/` directly. No broker needed; one round-trip per wake cycle
  (`GET /frame` + `POST /status`). Out of the box the wake cadence is
  **every 60 s**; the server's `/status` response can push a different
  `next_poll_s` per cycle or a durable `config.sleep_interval_s`, both
  clamped to `[30s, 7d]`. With SSE enabled a push cuts that wait short.
- **`mqtt`** — subscribes to a broker and reacts to retained frame
  announcements. Stays connected; pushes are near-instant. Requires a
  broker on the LAN.
- **`relay`** — for a panel that can't reach the Tesserae server at all.
  See [Cloud relay](#cloud-relay-remote-panels) below.

The installer prompts for transport at the top and only asks for the
relevant fields (REST → server URL + optional pairing code; MQTT →
broker host/port/credentials/client id).

Switching mode later is a config-file edit + `sudo systemctl restart
tesserae-pi-client`. **Existing `config.toml` files** without
`transport_mode` continue to default to **`mqtt`** (no surprise mode
switch on upgrade); only fresh installs get the new REST default.

### REST mode setup (default install)

When the installer asks for transport, hit Enter to accept `rest`,
then either:

1. **Recommended path (zero typing on the device):** start the daemon
   with no pairing code and click **Register** on the discovered row in
   the server's Settings → Devices page. The daemon's next
   `POST /device/discover` claims the token by MAC match; you'll see
   "registered via discover" in the journal.
2. **Strict path (per-device admin approval):** generate a 6-digit
   pairing code in Settings → Devices and enter it at the installer's
   pairing-code prompt. The code is single-use; after a successful
   register the daemon wipes it and saves the issued `device_token`.

To re-pair after the fact (e.g. token was revoked, or the local
`device_token` got wiped), generate a fresh code and run once with the
CLI override:

```bash
tesserae-pi-client --pair 123456
```

`--pair` overrides whatever is in `[rest].pairing_code` for that run
only and is no-op'd if a `device_token` is already saved.

If a 401 ever comes back from the server (token revoked or wiped from
the server side), the daemon clears the local `device_token` and exits.
Restart with `--pair` or rely on the discover loop to recover.

---

## MQTT contract

Active when `transport_mode = "mqtt"`. All topics are prefixed with the
configured `device_id` (default `pi_png`), i.e. `tesserae/<device_id>/...`.
The examples below use the default.

### Subscribe

Topic: `tesserae/<device_id>/frame/png` (QoS 1, not retained)

Payload (all five fields required):

```json
{
  "url": "http://192.168.1.10:8000/renders/3f7a91b2c4e5d6f8.png",
  "rotate": 0,
  "scale": "fit",
  "bg": "white",
  "saturation": 0.5
}
```

| Field | Type | Meaning |
|---|---|---|
| `url` | string | HTTP URL to GET the PNG. No auth. |
| `rotate` | int 0..3 | Quarter-turns CW to apply *before* scaling. Stacks with whatever rotation the server already baked in. |
| `scale` | string | One of `fit`, `fill`, `stretch`, `center`. |
| `bg` | string | Letterbox colour when `fit`/`center` leaves bars. One of `white`, `black`, `red`, `green`, `blue`, `yellow`, `orange`. Unknown names fall back to white. |
| `saturation` | float 0.0..1.0 | Passed straight to `inky.set_image(saturation=...)`. |

### Publish

Topic: `tesserae/<device_id>/status` (QoS 1, retained, also the LWT topic)

```json
{
  "state": "idle",
  "last_paint_at": 1734567890.123,
  "last_error": null,
  "last_digest": "3f7a91b2c4e5d6f8",
  "uptime_s": 3601,
  "fw_version": "0.1.0",
  "panel": "inky_impression_13_3",
  "kind": "pi_png_client",
  "panel_w": 1600,
  "panel_h": 1200,
  "ip": "192.168.1.42"
}
```

`state` is one of `idle`, `rendering`, `error`, `offline` (LWT).
Heartbeat: republished on every state change and at least every 60 s.

The `kind` / `panel_w` / `panel_h` / `ip` keys feed Tesserae's device
discovery: an unregistered `device_id` shows up under Settings → Devices as
a "Discovered" row, and these keys pre-fill the device kind and panel size
so registering it is one click. `panel_w` / `panel_h` are the post-rotation
dimensions the panel actually paints. `ip` is best-effort and blank if the
primary interface can't be determined.

---

## REST contract

Active when `transport_mode = "rest"`. All endpoints sit under `/api/v1/`
on the configured `server_url`. The client sends both `Authorization:
Bearer <token>` and `X-Tesserae-Token: <token>` on every authenticated
call (cheap belt-and-suspenders against header-stripping middleboxes).

### First boot — `POST /device/discover`

Body: `{device_id, kind: "pi_png_client", panel_w, panel_h, fw_version, mac}`.

The server responds in one of two shapes:

- `{registered: false, retry_after_s: 30}` — the device shows up in
  Settings → Devices "Discovered" strip; sleep `retry_after_s` and poll
  again.
- `{registered: true, device_token, device_id, server_time}` — admin
  clicked Register; persist the token, **adopt `device_id` from the
  response** (it may differ from what was sent — using the wrong one
  gives 403 on subsequent calls), then enter the wake loop.

### Alternative first boot — `POST /device/register`

Header: `X-Pairing-Code: <6-digit-code>`, body same as discover.
Returns `201 + {device_token, device_id, reused_existing}` on success;
`403` on bad/expired code (process exits — generate a fresh code);
`429 + Retry-After` if rate-limited.

### Wake loop — `GET /device/<id>/frame`

Header: `Authorization: Bearer <token>`, optional `If-None-Match:
<last_frame_etag>`.

- `200` + JSON `{url, format: "png", render_id, rotate, scale, bg,
  saturation, panel_w, panel_h}` and `ETag: "<sha256>"` — download `url`,
  apply the rotate/scale/bg transforms, paint with `saturation`, save the
  new ETag. The `rotate/scale/bg/saturation` fields use the same vocabulary
  as the MQTT payload.
- `304` — composition unchanged; skip download + paint.
- `204` — server hasn't rendered anything for this device yet.
- `401` — token invalid; the daemon wipes `device_token` from
  `config.toml` and exits.
- `403` — token doesn't match the device id (likely a stale local id);
  exits without wiping the token. Re-pair to refresh.

### Wake loop — `POST /device/<id>/status`

Body: the same heartbeat shape as the MQTT retained
`tesserae/<device_id>/status` payload (`{state, last_paint_at, last_error,
last_digest, uptime_s, fw_version, panel, kind, panel_w, panel_h, ip}`),
plus, when available, `rssi`, `tz` and `next_sleep_s`. Each optional field is
**omitted rather than zeroed** when it has no value, so the server can tell
"not reported" from "reported as none". `battery_pct` / `battery_mv` /
`power_plugged` appear only with a [PiSugar UPS configured](#battery-telemetry);
`wake_reason` stays absent, a Pi never deep-sleeps.

Response: `{status, config: {sleep_interval_s?, tz?}, next_poll_s?,
server_time}`. `config.sleep_interval_s` is durable (persisted to
`[rest].poll_interval_s` for future cycles), `next_poll_s` is one-shot
(only the next sleep duration). Both clamp to `[30s, 7d]`. `config.tz` is
durable too — persisted to `[rest].tz` and echoed back on every beat.

> The response also carries a **top-level** `tz` (alongside `local_time` /
> `tz_offset_seconds`). That one is an *echo* of whichever zone the server
> resolved — including one this client sent — so it is deliberately **not**
> adopted: a heartbeat `tz` overrides the server's setting, and adopting the
> echo would pin the panel to a stale zone the first time an operator
> changes the server's timezone. Only `config.tz` is treated as durable
> server-owned config.

A `429` on either the frame GET or the status POST honours `Retry-After`
(numeric seconds only), clamped to the same `[30s, 7d]` range so a bad
header can't pin the daemon at sub-second retries.

### Push updates — `GET /device/<id>/stream`

Headers: `Authorization: Bearer <token>`, `Accept: text/event-stream`.

The sibling ESP32 firmware gates this behind kiosk mode, because a
deep-sleeping battery device can't hold a socket open. A mains-powered Pi
can, which makes this the one firmware feature that fits *better* here: a
`sync` event cuts the sleep short and the normal frame GET runs immediately,
so a dashboard edit lands in about a second instead of up to a full poll
interval.

Only `sync` is acted on. `values` and `patches` drive on-device overlay
rendering, which needs a partial-refresh panel the Impression doesn't have —
they're ignored by design.

The stream is strictly an optimisation: polling carries the same
information, and every failure path (404 from an older server, dropped
connection, proxy buffering) degrades to plain polling. Reconnects back off
1→60 s with ±25 % jitter, and 60 s of silence — the server sends a keepalive
comment every 25 s — forces a reconnect.

**Reverse proxies must not buffer this route.** The server sets
`X-Accel-Buffering: no`, which nginx honours; Caddy needs
`flush_interval -1`. If your proxy buffers anyway, set
`[rest].sse_enabled = false` and the client polls as before.

### Front buttons — `?button=` on the frame GET

The Impression's four front buttons are read over libgpiod (active-low with
pull-ups, falling edge) and reported to the server, which maps
`refresh` / `left` / `right` onto refresh / rotate_prev / rotate_next.

Pin numbers differ by model: **A=5, B=6, D=24 on every Impression, but C is
GPIO 16 on the 4"/5.7"/7.3" and GPIO 25 on the 13.3"** — a difference
Pimoroni documents in their getting-started guide. This client has no
configured panel model, so it resolves `pin_c` at runtime from the
resolution `inky.auto()` reported; the live panel cannot disagree with
itself. Set `[buttons].pin_c` to a non-zero value to override.

Delivery follows the firmware's discipline so one press produces exactly one
server event:

1. A press rides the **frame GET** as `?button=<name>&button_event_id=<id>`,
   so the server dispatches the action *before* composing its response — the
   frame that comes back already reflects the press.
2. `200`, `204` **or `304`** all prove the request arrived, so the press is
   cleared before the status POST. (`304` counts: "nothing changed" is still
   an acknowledgement.)
3. Anything else — network error, `429`, `5xx` — retains it, and the status
   POST delivers `{button, button_event_id}` as the fallback path. The server
   dedups on the event id, so the double path can't double-fire.

`button_event_id` is seeded randomly at start-up rather than from zero: the
server dedups against the last id it saw, so a client that restarted and
replayed `0, 1, 2, …` would have its first presses silently swallowed.

Buttons are off by default. A HAT without them, or a Pi without libgpiod,
logs a warning and keeps painting frames — failing to read a button is never
allowed to stop the panel working.

## Local frame cache (decks + offline albums)

Both features are **capability-gated**: the client advertises
`deck_cache` / `frame_cache` (with the cache filesystem's free space) on
each heartbeat, and that is what makes the server offer manifests. Turn
`[cache].enabled` off and the wire body is byte-identical to a client
that never had a cache.

**Decks.** `GET /device/<id>/deck` returns pages with 16-hex sha256
digests, byte sizes, TTLs and a button/zone link graph. Frames land in
`~/.cache/tesserae-pi-client/decks/<deck_id>/`, and a button press
the graph can answer paints straight from disk — no server round trip.

A locally-served press is deliberately **not** reported as a button
action: the server saw no request, so reporting it would double-count a
press already resolved on-device. What *is* reported is
`deck_page_id` + `deck_version`, so the server's navigation state stays
truthful.

**Albums.** `GET /device/<id>/collection` returns a schema-1 album:
frames with positions, digests and URLs, plus a playback block
(`sequential`/`shuffle` × `loop`/`reshuffle`/`once`, and an
`interval_s` the client clamps to `[60s, 24h]` — e-ink takes seconds to
refresh and has a rated refresh count). Playback runs from the local
cache, so the panel keeps cycling photos while the server is down.

A one-off dashboard frame interrupts the album until its **next**
scheduled deadline, without pushing that deadline further out — else a
chatty dashboard would starve playback indefinitely.

Every cached frame is verified before it is painted, and the writer
re-hashes before storing. Any miss, mismatch, corruption or failed decode
falls back to the network path silently. The cache is an optimisation,
never a requirement.

Unlike the firmware there is no 32-frame limit; that was an ESP32 memory
constraint. The server still decides what to mark cacheable from the
capacity we advertise.

### Verifying a frame without a render payload

This is the one place the two Pi clients genuinely differ. The
[`.bin` client](https://github.com/dmellok/tesserae-device-pi-bin) paints a
headerless packed 4-bpp buffer whose length is *exactly*
`panel_w * panel_h / 2`, so it can reject a bad relay or cache frame with a
single length comparison. A PNG has no fixed length, so on the paths where
bytes arrive **without** a trusted `{url, rotate, scale, bg, saturation}`
announcement — the cache and the relay — `png_frame.py` substitutes three
structural checks:

1. the **PNG signature**, which catches a proxy error page or a truncated
   download before the decoder ever sees it;
2. a **real decode** through PIL with `load()` forced, so corrupt image data
   fails here rather than halfway through a panel refresh;
3. the **decoded dimensions against `paint.panel_resolution()`** — the live
   panel, not the relay's plaintext `X-Tesserae-Panel-*` headers, which the
   relay could have altered.

The dimension check is exact, matching the strictness of the length check it
replaces: frames on these paths are pre-rendered for this panel from the
geometry it self-reported, so a different size means the two ends disagree
about the hardware. Cached frames are additionally checked against the
manifest's `bytes` and their content digest, which port unchanged.

The ordinary REST/MQTT frame path does **not** go through this: it carries
the server's transform hints, and the [transform
pipeline](#transform-pipeline) is what turns an arbitrary image into a
panel-sized one.

## Cloud relay (remote panels)

For a panel that lives where the Tesserae server isn't reachable — a
different house, a phone hotspot, behind CGNAT. Both ends talk
**outbound** to a small Cloudflare Worker acting as a per-device
mailbox: home seals each rendered frame and `PUT`s it, this client polls
and decrypts. Nothing at home is exposed to the internet.

The relay is **zero-knowledge**. At pairing the panel and the home
instance exchange X25519 *public* keys through it and each derive the
same AES-256-GCM frame key locally; the key is never transmitted. The
relay only ever holds ciphertext, two public keys and a token hash.

```
frame_key = HKDF-SHA256(X25519(priv, their_pub),
                        info="tesserae-relay-frame-key-v1", L=32)
sealed    = nonce(12) || AES-256-GCM(frame, frame_key, nonce, aad="")
```

Setup:

```toml
transport_mode = "relay"
```

```sh
tesserae-pi-client --pair CODE   # code from Settings -> Cloud relay
```

The panel self-reports `model: "pi_png_client"` plus the detected
`panel_w`/`panel_h` when it pairs, so home renders PNGs at this panel's
geometry without the operator pre-entering anything.

Unlike REST pairing, the relay code is **persisted** — the rendezvous
can span restarts by design, since home may take a while to complete the
exchange. The `POST /v1/pair` happens exactly once (the stored private
key is the latch): only that route enforces the code's expiry, so
re-posting later would 404 a code that is still perfectly good for
polling. Once the frame key is derived the private scalar is wiped.

Two safety rules the implementation holds to:

- **A failed GCM authentication tag is never painted.** It means the
  mailbox doesn't match this panel's key, and painting it would render
  whatever an attacker or a corrupted store supplied.
- **The decrypted payload is verified structurally** — signature, decode,
  dimensions against the detected panel — rather than against the relay's
  plaintext `X-Tesserae-*` headers. See [Verifying a frame without a render
  payload](#verifying-a-frame-without-a-render-payload).

A `401` on any device-token route means the pairing was **revoked**
(a revoked device's mailbox and token record are deleted, so a freshly
paired panel with nothing published sees `204`, never `401`). The client
drops its stored pairing rather than retrying a dead token — and the
config it writes in that state parses back cleanly, so a restart after
revocation still starts.

Buttons work differently here: a relay panel's frame GET terminates at
the relay, so the `?button=` query can't reach home. The press rides the
**status body** instead, and is repeated on *every* status post of the
wake — the relay's status slot is latest-only, so an idle beat could
otherwise overwrite a button-carrying post before home pulls it. Home
dedups on the event id, so repeating is safe.

The wire contract is `docs/relay/contract.md` in the Tesserae repo, and
it is authoritative for all three sides. Its golden vectors are pinned
in [tests/test_relay_crypto.py](tests/test_relay_crypto.py) — a
derivation that is merely *plausible* round-trips against itself while
never agreeing with home, and the only field symptom is that frames
silently never appear.

## OTA state reporting

This client **does not install anything**. What it does is report update
attempts to the server's Devices UI, using the same `ota` object the
firmware sends:

```json
{ "ota": { "schema": 1, "phase": "confirmed", "reason": "ok",
           "target_fw": "1.6.0", "attempt_id": "a3f19c" } }
```

`schema` alone means idle, and is the capability advertisement that
makes the server willing to offer an update at all. Whatever performs
the update (apt, pip, git, a systemd timer) records progress through
`OtaReporter`; state is persisted, because the transition worth having —
`pending_confirm` → `confirmed`/`rolled_back` — spans the restart it is
waiting on. Set `[ota].report_enabled = false` to stop advertising.

`fw_version` is reported as plain SemVer. A source checkout used to emit
`0.0.0+unknown`; a PEP 440 local-version segment doesn't parse as SemVer,
so the server's update comparison rejected it outright.

## Environment telemetry

Optional ambient temperature/humidity from an **SHT4x** or **SHTC3** on
the HAT's existing I²C bus (already enabled for the HAT EEPROM):

```toml
[environment]
sensor = "sht4x"
```

Reports `temperature_c`, `humidity_pct` and `env_sensor` — the same
fields the reTerminal E-Series sends. Readings are CRC-checked (a
marginal I²C bus returns plausible-looking garbage, and a wrong
temperature is harder to notice than a missing one), and a failed read
omits the fields rather than interrupting the heartbeat.

The Pi's own CPU temperature is deliberately **not** reported here: the
server's field means ambient room temperature, and a SoC running 30 °C
above the room would quietly poison anything plotted from it.

## Battery telemetry

A Pi has no battery of its own, so the heartbeat leaves the battery fields
out and the server shows the device as mains-powered. Add a **PiSugar 3**
(or 3 Plus) UPS and the panel keeps painting through a power cut; tell the
client how to read it and the server's battery tile, history chart,
`device_battery` widget and Home Assistant battery / low-battery sensors
light up, the same way they do for the ESP32 firmware:

```toml
[battery]
source = "pisugar3"          # or "pisugar-server"
```

`pisugar3` reads the gauge straight off the HAT's I²C bus (address `0x57`,
the shipped default): three registers, nothing to install. `pisugar-server`
asks PiSugar's own power manager on `127.0.0.1:8423` instead, for setups
that already run it for the web UI and would rather one process owned the
bus. Both report `battery_pct`, `battery_mv`, `battery_source` and
`power_plugged`, the last of which is what tells a UPS sitting at 100 %
apart from a panel running down.

The gauge's own percentage is reported, never one derived from voltage:
PiSugar's curve is the one its charge-protection cap (about 80 % when
enabled) is measured against. Reads are plausibility-checked and
best-effort; a failed read omits the fields for that beat and the tile
falls back to "Mains". The PiSugar manager polls the same bus, so a
collision costs one beat rather than being latched. The fields ride the
REST status POST only, like the environment sensor.

---

## Transform pipeline

For each arriving frame:

1. Download the PNG via plain HTTP (size capped at `max_frame_bytes`).
2. Decode with PIL, coerce to mode `RGB`.
3. Rotate by `rotate * 90°` clockwise (`expand=True` so nothing is cropped).
4. Scale per `scale` mode:
   - `fit` — preserve aspect, letterbox with `bg` colour
   - `fill` — preserve aspect, crop to cover
   - `stretch` — `resize` straight to panel size, distorts
   - `center` — paste at native size, letterbox, crop overflow
5. Hand to `panel.set_image(img, saturation=...)`, then `panel.show()`.

---

## Troubleshooting

**`could not auto-detect inky panel` / `No EEPROM detected`**
- enable **both** SPI and I2C — the panel ID lives in the HAT EEPROM, which
  `inky.auto()` reads over I2C:
  `sudo raspi-config nonint do_spi 0 && sudo raspi-config nonint do_i2c 0`
- **reboot**, then confirm the EEPROM is visible:
  `ls /dev/i2c-1 && sudo i2cdetect -y 1` (expect `50` in the grid)
- check your user is in `gpio`, `spi`, and `i2c` groups: `groups`
  (`i2c` is needed for the EEPROM read, not just the SPI pixel path)
- if `i2cdetect` shows no `50`, the board has no readable EEPROM (some
  Impression/Spectra units, all non-genuine boards) and auto-detect can't
  identify it

**`Woah there, some pins we need are in use!` / `Chip Select … claimed by spi0 CS0`**
- The panel auto-detects fine but paint fails: the kernel SPI driver reserves
  GPIO8 (CS0), while recent `inky` drives chip-select in software.
- Free the pin with the zero-chip-select overlay, then reboot:
  ```bash
  CONFIG=/boot/firmware/config.txt; [ -f "$CONFIG" ] || CONFIG=/boot/config.txt
  grep -q '^dtoverlay=spi0-0cs' "$CONFIG" || echo 'dtoverlay=spi0-0cs' | sudo tee -a "$CONFIG"
  sudo reboot
  ```
- `scripts/install.sh` does this automatically; this is for manual installs.

**Frame never paints, state stays `idle`**
- Is the broker reachable from the Pi? `mosquitto_sub -h <broker> -t '#'`
- Is the URL reachable from the Pi? `curl -I <url>`
- Tail the journal: `sudo journalctl -fu tesserae-pi-client`

**`state: error` with `last_error` ending in `URLError` / `TimeoutError`**
- The Tesserae server's render directory is unreachable from the Pi —
  check firewall, port, and that the server is actually serving on the
  URL the message advertised.

**REST: `not registered yet — admin needs to click Register on the server` loops forever**
- The daemon's `POST /device/discover` is reaching the server, but no one
  has clicked Register in Settings → Devices. Either click it (the next
  discover poll claims the token) or mint a 6-digit pairing code and rerun
  with `--pair <code>` for the strict path.

**REST: `frame GET 401: token invalid`**
- The server revoked the device or the local `device_token` is corrupt.
  The daemon wipes it from `config.toml` and exits — re-pair (`--pair`)
  or wait for the discover loop to re-claim.

**REST: `frame GET 403: token not valid for this device`**
- The local `[mqtt].device_id` no longer matches the server's canonical
  id (admin renamed the device). The discover-claim flow normally adopts
  the new id; if you got here, re-pair to refresh both id and token.

**REST: log shows `server_time skew=...s; check NTP`**
- The Pi's clock has drifted more than a minute from the server's. We
  don't `settimeofday` (NTP owns that on Linux); check that
  `chronyd`/`systemd-timesyncd` is running.

**Relay: `relay pair 403: rejected by the relay`**
- Most often this is *not* an auth problem. The hosted relay sits behind
  Cloudflare bot protection, which blocks known-bot user agents before the
  request reaches the Worker. The client sends a descriptive `User-Agent`
  for exactly this reason; if you've put a proxy in front that rewrites it
  to something like `python-requests/2.x`, the 403 comes from the edge.

**Relay: frames never appear, no errors in the journal**
- Check the journal for `relay frame rejected`. A failed GCM tag means the
  mailbox doesn't match this panel's frame key — re-pair. A "refusing to
  paint a frame rendered for different hardware" line means home is
  rendering at a different size than `--paint-test` reports; re-pair so the
  panel re-reports its geometry.

**Buttons do nothing**
- They're off by default: set `[buttons].enabled = true`.
- Check the journal at startup for a `buttons armed: GPIO5->left, …` line.
  `could not open GPIO lines` means libgpiod isn't available or the user
  lacks access; the daemon logs it and keeps painting frames either way.
- Button C's pin differs by panel and is derived from the detected
  resolution. If your board is one we haven't catalogued, set
  `[buttons].pin_c` explicitly (16 on the 4"/5.7"/7.3", 25 on the 13.3").

**Colours look washed out / oversaturated**
- Tune the `saturation` field on the Tesserae server side. The Pi just
  passes whatever value arrives straight to `inky.set_image()`.

---

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check src tests
mypy src
```

The `paho-mqtt`, `Pillow`, `requests` and `cryptography` deps install
everywhere; `inky[rpi]` is Linux-only and won't install on macOS — that's
fine, `paint.py` lazy-imports it so the unit tests run on any host. `gpiod`
(buttons) and `smbus2` (ambient sensor) are lazy-imported too and only
needed on a Pi that has those features turned on.

### Layout

```
src/tesserae_pi_client/
  transforms.py        # pure rotate/scale/bg — fully tested
  png_frame.py         # signature + decode + geometry checks for payload-less frames
  config.py            # TOML load + atomic save + transport_mode/[rest]/[relay]/...
  paint.py             # inky wrapper (lazy-imported)
  mqtt_loop.py         # paho client + frame dispatcher
  heartbeat.py         # retained status + LWT + rssi/tz/next_sleep_s
  button_report.py     # exactly-once press delivery (pure logic)
  buttons.py           # libgpiod watcher (lazy-imported)
  deck.py              # deck manifest parse + navigation (pure)
  collection.py        # album manifest parse + playback traversal (pure)
  frame_cache.py       # content-addressed on-disk store
  cache_sync.py        # deck/album orchestration over the cache
  ota_report.py        # OTA lifecycle reporting (report only, never installs)
  environment.py       # optional SHT4x/SHTC3 ambient telemetry
  relay_crypto.py      # X25519 + HKDF + AES-GCM, pinned to the contract vectors
  transports/
    mqtt.py            # MQTT wake loop (extracted from main)
    rest.py            # REST polling loop + discover/register/pair
    relay.py           # cloud-relay pairing + poll loop
    sse.py             # SSE push channel (optimisation over polling)
  main.py              # CLI entry point, signal handlers, transport dispatch
```

License: AGPL-3.0-or-later.
