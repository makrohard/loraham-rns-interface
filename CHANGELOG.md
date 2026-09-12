# 0.2.0
- RF log: `rf_log = yes|no` + `rf_log_path` in the interface section append
  one line per packet the radio received (RSSI/SNR, before RNS sees it) or
  sent (`ok` on the radio's TX-done, `unconfirmed` when the window elapsed —
  the airtime was charged and the packet may have gone out). A packet dropped
  for duty or size writes nothing. Copy-truncate at 5 MB to `<path>.1`, same
  inode. `yes` without an absolute path refuses the interface.

# 0.1.0
- Direct-SPI LoRa interface for Reticulum.
