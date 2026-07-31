#!/usr/bin/env python3
"""Enable Sideband's plugin loader and point it at the lhpc plugin directory.

Sideband gates plugin loading behind two keys in its own msgpack config store
(`service_plugins_enabled` and `command_plugins_path`), which are otherwise only
reachable through the GUI settings — useless on a headless or scripted install.

Sideband fills any missing key with its default when loading, so writing a
PARTIAL config is safe; we never rewrite keys we do not own.

Usage: enable_sideband_plugins.py <sideband-config-dir> <plugins-dir>
"""

import os
import stat
import sys

# Sideband serialises its config with RNS's VENDORED umsgpack
# (core.py: `import RNS.vendor.umsgpack as msgpack`), not the PyPI package —
# which is not installed in its venv. Use the same one so we read and write
# byte-identically to Sideband.
try:
    import RNS.vendor.umsgpack as msgpack
except ImportError:                      # pragma: no cover - fallback only
    import msgpack


def main(argv):
    if len(argv) != 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    config_dir, plugins_dir = argv[1], argv[2]
    path = os.path.join(config_dir, "app_storage", "sideband_config")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.makedirs(plugins_dir, exist_ok=True)

    config = {}
    if os.path.isfile(path):
        try:
            with open(path, "rb") as fh:
                config = msgpack.unpackb(fh.read())
        except Exception as exc:
            # NEVER replace state we could not read. Sideband's config holds the
            # operator's settings — including connection IFAC passphrases — so
            # overwriting it with a two-key partial silently destroys them.
            print(f"existing Sideband config at {path} is unreadable ({exc}); "
                  f"refusing to overwrite it — fix or remove it, then rebuild",
                  file=sys.stderr)
            return 1
        if not isinstance(config, dict):
            print(f"existing Sideband config at {path} is not a config map "
                  f"({type(config).__name__}); refusing to overwrite it", file=sys.stderr)
            return 1

    # Preserve everything already there; we own exactly these two keys.
    config["service_plugins_enabled"] = True
    config["command_plugins_path"] = plugins_dir

    # Keep the existing mode; default owner-only. The previous version created the
    # temp file under the process umask and renamed it over the original, so a 0600
    # config could come back as 0644 — with passphrases in it.
    try:
        # CLAMP to owner-only. Preserving the existing mode exactly kept an already
        # world-readable config at 0644 — and that file can hold connection IFAC
        # passphrases. A stricter existing mode (0400) is preserved.
        mode = stat.S_IMODE(os.stat(path).st_mode) & 0o600
    except OSError:
        mode = 0o600
    if not mode:
        mode = 0o600

    directory = os.path.dirname(path)
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        try:
            os.write(fd, msgpack.packb(config))
            os.fsync(fd)                       # data on disk before it becomes the config
        finally:
            os.close(fd)
        os.chmod(tmp, mode)                    # defeat the umask, explicitly
        os.replace(tmp, path)
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)                      # persist the rename itself
        finally:
            os.close(dfd)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        print(f"could not write {path}: {exc}", file=sys.stderr)
        return 1
    print(f"sideband plugins enabled -> {plugins_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
