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

## Licence

MIT for this repository. RNS is under the Reticulum License and is imported, not
copied — see `LICENSE`.
