# 0.3.0
- `rnode_framing = yes|no` (default `no`): the RNode firmware's one-byte air
  header on every frame, packets up to 508 bytes split over two frames and
  reassembled by sequence on receive, so a box can exchange packets with an
  RNode. `HW_MTU` follows (508 framed, 255 bare); while framing is on the
  preamble defaults to what the firmware programs for the same SF/BW/CR (18 at
  SF8/BW125) — an SX127x receiver needs at least that to hear an RNode at all.
  The RF log keeps showing the air bytes, one line per air frame.

# 0.2.1
- RF log: one lock around rollover and write — the RX and TX threads could race two
  rollovers and lose the previous segment.

# 0.2.0
- RF log: `rf_log = yes|no` + `rf_log_path` in the interface section append
  one line per packet the radio received (RSSI/SNR, before RNS sees it) or
  sent (`ok` on the radio's TX-done, `unconfirmed` when the window elapsed —
  the airtime was charged and the packet may have gone out). A packet dropped
  for duty or size writes nothing. Copy-truncate at 5 MB to `<path>.1`, same
  inode. `yes` without an absolute path refuses the interface.

# 0.1.0
- Direct-SPI LoRa interface for Reticulum.
