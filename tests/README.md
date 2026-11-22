# Tests

A test fails because the interface did the wrong thing, never because a name, a refactor or a
sentence changed. The rules are LoRaHAM Pi Control's test rules
([tests/README.md, "The rules"](https://github.com/makrohard/loraham-pi-control/blob/main/tests/README.md#the-rules)),
checked by its `tools/check_houserules.py` with this repository's [`.houserules.toml`](../.houserules.toml).

## Contents

- [Layers](#layers)
- [Checks before a change lands](#checks-before-a-change-lands)

## Layers

A test goes in the folder of the behaviour it protects, never in a file named after when or how a
defect was found.

| folder | proves |
|---|---|
| `spi/` | the direct-SPI driver: profiles, bus and chip select, the SX126x/SX127x drivers, the SPI interface's limits |
| `shared/` | what both backends share: duty ledger, power and segment policy, RNode framing, RF log |
| `daemon/` | the daemon backend |
| `node/` | the service runner (`loraham-rns-node`) and `loraham-rns-client` |
| `plugin/` | the Sideband location plugin |

No radio, no root, no network: the hardware is faked (`spi_interface.py` builds the SPI interface
without a bus), clocks are injected, waits are ordered by events.

## Checks before a change lands

```sh
pip install .[dev]
ruff check loraham_rns interfaces plugins scripts
ruff check tests --select F,E9
python -m pytest -q
python <loraham-pi-control>/tools/check_houserules.py <base>..<head>
```

A HARD line of the house-rules check blocks; a WARN line is the reviewer's to judge. A commit that
adds a test says in its message how the test failed before the change (red-before).
