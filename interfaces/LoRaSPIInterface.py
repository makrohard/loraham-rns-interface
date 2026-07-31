# Loaded by RNS from <configdir>/interfaces/. RNS execs this file and takes the
# module-level `interface_class`; the implementation lives in the installed
# package so it can be imported and unit-tested normally.
from loraham_rns.interface import LoRaSPIInterface

interface_class = LoRaSPIInterface
