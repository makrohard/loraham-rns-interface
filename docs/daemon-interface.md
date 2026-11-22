# The daemon interface: the contract with its controller

`LoRaDaemonInterface` reaches the radio through the LoRaHAM daemon's sockets instead of driving it
over SPI. This page is the contract between the interface and the controller that writes its
configuration section (LoRaHAM Pi Control): names, keys, defaults, the pre-check, health and detach.
The air format, duty accounting, power policy, RNode framing and RF log are the ones the direct-SPI
interface uses ([README](../README.md)).

## Contents

- [Names](#names)
- [The section](#the-section)
- [What is sent to the daemon](#what-is-sent-to-the-daemon)
- [The pre-check](#the-pre-check)
- [Health, the runner and detach](#health-the-runner-and-detach)

## Names

| what | value |
|---|---|
| interface type | `LoRaDaemonInterface` |
| interface file | `interfaces/LoRaDaemonInterface.py`, installed beside `LoRaSPIInterface.py` in the node's `interfaces/` directory |
| module | `loraham_rns.daemon` |
| section name | `LoRa <rid>`, e.g. `[[LoRa r2]]`: the name `rnstatus --attach/--detach/--reload` and `rnstatus -j` use |
| daemon | 1.6.0 or later (the `RADIO=` health lines) |

## The section

The section is the whole truth at connect: a key that is missing takes the default below, nothing
else is read. A value outside its range refuses the interface (RNS logs the reason and carries on
without it). Numbers are decimal; switches are `yes`/`no` (also `on`/`off`, `true`/`false`, `1`/`0`).

| key | type, range | default | daemon |
|---|---|---|---|
| `type` | `LoRaDaemonInterface` | required | — |
| `interface_enabled` (or `enabled`) | switch, read by RNS | RNS's | — |
| `band` | `433` or `868` | required | — |
| `conf_socket` | absolute path: `<dir>/loraconf-<rid>.sock` | required | CONF socket |
| `data_socket` | absolute path: `<dir>/loraf-<rid>.sock` (the FRAMED socket) | required | DATA socket |
| `frequency` | Hz; the whole occupied bandwidth inside one permitted segment of `band` | required | `FREQ` (MHz) |
| `bandwidth` | Hz: 7800, 10400, 15600, 20800, 31250, 41700, 62500, 125000, 250000, 500000 | 125000 | `BW` (kHz) |
| `spreadingfactor` | 7–12 | 8 | `SF` |
| `codingrate` | 5–8 (4/5–4/8) | 5 | `CR` |
| `txpower` | dBm, 2 up to the band cap (868: 14, 433: 10) | the band cap | `POWER` |
| `preamble` | symbols, 6–512 | with `rnode_framing = yes` the RNode firmware's value for SF/BW/CR (18 at SF8/BW125), else 8 | `PREAMBLE` |
| `syncword` | 0x00–0xFF (hex or decimal) | 0x12 | `SYNC` |
| `crc` | switch | yes | `CRC` |
| `ldro` | `auto`, `0`, `1` | `auto` | `LDRO` |
| `txmode` | `managed` or `direct` | `managed` | `SET TXMODE` |
| `txqueue` | switch (`1`/`0`, `yes`/`no`) | 1 | `SET TXQUEUE` |
| `cadidle` | ms, 0–2000 | 50 | `SET CADIDLE` |
| `cadwait` | ms, 50–5000 | 1500 | `SET CADWAIT` |
| `cadrssi` | dBm, -130–0 | -90 | `SET CADRSSI` |
| `cadmonitor` | switch | no | `SET CADMONITOR` |
| `cadtxaftertimeout` | switch | no | `SET CADTXAFTERTIMEOUT` |
| `rnode_framing` | switch | no | — |
| `airtime_limit_short` | % of the 15 s burst window, >0–100 | 33.0 | — |
| `airtime_limit_long` | % of the hour, >0 up to the segment's duty ceiling | 868: 1.0, 433: 10.0, at most the ceiling | — |
| `state_dir` | directory of the duty ledger `airtime.json` | the working directory | — |
| `rf_log` | switch | no | — |
| `rf_log_path` | absolute path | required with `rf_log = yes` | — |
| `networkname`, `passphrase` | IFAC: both or neither | none | — |

The direct-SPI keys (`hardware`, `spidev`, `gpio_chip`, `runtime_dir`, `spi_speed`) are ignored;
the generic RNS keys (`mode`, `outgoing`, `ifac_size`, announce rates, ...) are RNS's own.

## What is sent to the daemon

At every connect, and again whenever the radio returns to `READY`, the interface sends on the CONF
socket, each line answered `OK` before the next:

1. `SET TXRESULT=1` — always; the interface needs a result per frame. Not a setting.
2. `SET TXMODE=…`, `SET TXQUEUE=…`, `SET CADIDLE=…`, `SET CADWAIT=…`, `SET CADRSSI=…`,
   `SET CADMONITOR=…`, `SET CADTXAFTERTIMEOUT=…` — one line each, from the section.
3. `SET MODE=LORA FREQ=… BW=… SF=… CR=… POWER=… PREAMBLE=… SYNC=… CRC=… LDRO=…` — one line, from
   the section.

Nothing else: without a controller the interface runs on the defaults above, never on the
daemon's boot profile. A refused line (`ERR …`) leaves the interface offline, the reason logged,
and it connects again: the daemon answers `SET TXQUEUE=0` with `ERR BUSY` while queued frames are
pending, so it applies once they are done. No frame goes out before every line is answered `OK`.

## The pre-check

```sh
.venv/bin/loraham-rns-check-section FILE
.venv/bin/loraham-rns-check-section < section
```

reads one section (from FILE, from stdin when FILE is absent) as the node does, with RNS's own
config reader (the `[[…]]` header is optional, a key is given once) and exits 0 when the interface
would accept it, or 1 with one line on stdout naming the key and the limit it breaks. It runs the same code the
interface's constructor runs, so a section that passes is not refused for its values. Exit 2 on a
usage error or a FILE that cannot be read (UTF-8).

## Health, the runner and detach

- **Online** while the daemon reports `READY` (`GET STATUS` at connect, then the `RADIO=` lines);
  **offline** on any other state or a lost socket. Offline is not a fault: packets wait in the
  queue up to 120 s (then they are dropped, logged), the interface reconnects every 5 s, and on
  `READY` it sends its settings again.
- **The runner:** `loraham-rns-node --interface "LoRa <rid>"` names the node owner's section. It
  starts only when that interface is online: a radio not `READY` at start means no ready marker,
  exit 4, and the controller's start fails. Running, it exits when the interface reports a fault; a
  daemon radio that goes offline later is not a fault.
- **`detach()`** stops both worker threads, closes both sockets and the RF log, and keeps the duty
  ledger. A later attach of the same section (`rnstatus --attach`) connects anew.
