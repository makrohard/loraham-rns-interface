# loraham-rns-interface

A **direct-SPI LoRa interface for Reticulum** on LoRaHAM Pi hardware: RNS drives
the radio itself over `spidev` + `libgpiod`. No rnoded, no RNode firmware, and no
KISS layer — one LoRa packet is one RNS packet, so the radio's own PHY framing
does the job KISS does over a serial link.

Built for [loraham-pi-control](https://github.com/makrohard/loraham-pi-control),
which ships it as the `reticulum` stack.

## Why a service runner

`rnsd` is not safe to supervise directly. RNS catches an external-interface
initialisation exception, logs it and carries on — so a failed bus lock or a dead
radio leaves a process running with its shared-instance ports open and no radio
behind them. A second `rnsd` also attaches to an existing instance as a *client*
and keeps running, loading no interfaces at all.

`loraham-rns-node` refuses both: it exits non-zero unless it became the
shared-instance **owner** and the named interface is **online**, and it exits if
the radio goes fatal later.

```
loraham-rns-node --config <rns-config-dir> --interface "LoRa 868"
  exit 3  attached to a foreign shared instance
  exit 4  interface missing or never came online
  exit 5  bus/lock/radio fault while running
```

## Sharing the SPI bus

The LoRaHAM daemon treats `<LORAHAM_RUNTIME_DIR>/spi0.lock` as a fail-closed
contract: no SPI transfer may proceed unless that `flock` is held, bounded at
2 s, fatal otherwise. This driver is a peer on that bus and honours the same
rules, so the daemon's invariant still holds while Reticulum runs. One
transaction is `acquire lock → assert CS → transfer → deassert CS → release`,
and the lock is never held across a wait loop.

Chip-select is driven by us (soft CS), which is the mode lhpc's
`bootstrap-deps.sh --spi-mode soft-cs` configures. Claiming the line through
libgpiod also re-muxes it away from the SPI controller, which is what guarantees
the kernel cannot assert it in parallel.

## Hardware

Pins, chip, TCXO and PA settings come from a profile table keyed on the lhpc
hardware setup and band — they are not free-form config, because a wrong PA or
TCXO value can damage the module.

| setup | band | chip | notes |
|---|---|---|---|
| `loraham` | 433 / 868 | SX1278 / SX1276 | RESET wired |
| `uputronics` | 433 / 868 | SX127x | **no RESET line** — soft reset |
| `waveshare-433/868` | 433 / 868 | SX1262 | DIO2 RF switch; TCXO probed, crystal fallback |

**SX1262 verified on 868** against an SX1276 peer, both directions — sync word
`0x14 0x24` (RadioLib encoding) confirmed read back from the chip. The exact
commit set for that run is recorded in the consumer's `docs/test-matrix.md`
(a commit cannot cite its own hash). `waveshare-433` is code-complete but
untested: the board tested here is 868-only.

The driver probes for a TCXO on DIO3 and falls back to the crystal — the board
tested has none, and without the fallback `SetTx` is accepted while the chip
stays in `STBY_RC` (`XOSC_START_ERR`).

## Duty cycle

Airtime is reserved *before* transmitting and persisted, so a restart or crash
loop cannot wipe the hour's accounting. Corrupt state blocks TX but never RX; an
unconfirmed transmission stays charged, because we cannot prove nothing was
radiated.

## RNode air framing

By default one LoRa payload *is* one Reticulum packet, which is what two LoRaHAM
boxes expect of each other. The official RNode firmware does not: it puts one
header byte in front of every LoRa packet (a random 4-bit sequence plus a split
flag) and strips it on receive, and it sends packets over 254 bytes as two
frames that carry the same header. Bare packets and framed packets cannot be
told apart on the air, so an RNode and a bare box never exchange a packet even
with identical radio settings — the box sees the header as part of the packet,
the RNode eats the box's first byte as a header.

```
  [[LoRa]]
    rnode_framing = yes
```

turns the driver into what the firmware expects: the header goes on every
outgoing frame, packets up to the firmware's 508-byte MTU are split as it
splits them, and on receive the header is stripped and split packets are
reassembled by sequence before Reticulum sees them. Default `no`; a value that
is neither refuses the interface. Set it the same on every station that shares
the channel: a framed box and a bare box are as deaf to each other as an RNode
and a bare box.

`rnode_framing = yes` also sets the preamble, unless `preamble` is given, to
what the RNode firmware programs for the same SF/BW/CR: symbols for a 24 ms
target (6 ms above 30 kbps), never fewer than 18 — 18 at SF8/BW125, 24 at
SF7/BW125, 94 at SF7/BW500. An SX127x receiver only locks when its own
preamble setting is at least as long as the transmitter's (measured on the 433
and 868 modules at SF8/BW125: programmed with 8 the box heard nothing from an
RNode; with 18, every frame). The SX126x side does not care.

Two deliberate departures from the firmware, both on the side of discarding: a
pending first fragment older than four full frames' airtime (never under 5 s)
is dropped rather than glued onto a later fragment that happens to reuse its
sequence; and the header-only trailer the firmware sends after an exact
508-byte packet is never delivered and also discards a pending half — the
firmware would complete a half whose partner was lost with zero bytes.

## RF log

Two keys in the interface section, both written by the controller:

```
  [[LoRa]]
    rf_log = yes
    rf_log_path = /absolute/path/rf-reticulum.log
```

One line per packet the radio received or sent, at the radio boundary — before
RNS decides what an incoming packet is, and with the radio's own confirmation as
the outcome of an outgoing one:

```text
<utc> RX rssi=<dBm> snr=<dB> len=<n> hex=<..> ascii="<..>"
<utc> TX rssi=- snr=- len=<n> outcome=<ok|unconfirmed> hex=<..> ascii="<..>"
```

The payload is the raw LoRa payload: Reticulum ciphertext, IFAC included, and
MeshChat traffic looks like every other packet. `unconfirmed` means the radio
did not report TX done within the window; the airtime was charged and the
packet may have gone out, so it is never logged as not radiated. A packet
dropped for duty or size writes nothing. With `rnode_framing = yes` every
logged frame starts with the RNode header byte and a split packet is two
lines, because the log shows one line per air frame: a split packet is two lines, and a
received exact-508-byte packet from an RNode is three, including the firmware's
header-only trailer. The file is copy-truncated at 5 MB
into `<path>.1` on the same inode, so truncating it externally is safe. `rf_log
= yes` without a path, or a relative path, refuses the interface — the runner
exits rather than running unlogged. Default `no`.

## Licence

MIT for this repository. RNS is under the Reticulum License and is imported, not
copied — see `LICENSE`.
