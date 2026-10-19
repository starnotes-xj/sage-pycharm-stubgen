"""Build the slim sidecar from the installed stub tree and deploy it.

Measures the real-corpus effect of the A+B slimming (documentation into
64 hash buckets, per-source digests collapsed into the top-level table)
and installs the result into the live PyCharm configuration directory.

Usage (WSL sage env):  PYTHONPATH=src python scripts/wsl_deploy_slim_sidecar.py
"""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from sage_pycharm_stubgen.sidecar import (  # noqa: E402
    DOCS_DIRECTORY,
    build_sidecar_files,
    install_sidecar,
)


def main() -> int:
    import sage

    # An explicit stub root (the regen output tree) avoids the installed
    # tree's experimental symlinks, which the generator cannot resolve.
    if len(sys.argv) > 1:
        pkg = Path(sys.argv[1]) / "sage"
    else:
        pkg = Path(sage.__file__).parent
    print(f"stub tree: {pkg}")

    files = build_sidecar_files(pkg, "10.9", "3.13")

    index_bytes = len(files["sage-api-index.json"].encode("utf-8"))
    bucket_files = {
        name: text for name, text in files.items() if name.startswith(f"{DOCS_DIRECTORY}/")
    }
    bucket_bytes = sum(len(text.encode("utf-8")) for text in bucket_files.values())
    nonempty = [name for name, text in bucket_files.items() if text]
    largest = max(bucket_files, key=lambda n: len(bucket_files[n].encode("utf-8")))
    receipt = json.loads(files["artifact-receipt.json"])

    print(f"slim index: {index_bytes / 1e6:.1f} MB (was 103.9 MB)")
    print(f"buckets: {len(nonempty)}/64 nonempty, {bucket_bytes / 1e6:.1f} MB total, "
          f"largest {largest} at {len(bucket_files[largest].encode('utf-8')) / 1e6:.2f} MB")
    print(f"documents moved: {receipt['documentation']['entries']}")
    print(f"index entries: {json.loads(files['sage-api-index.json'])['entries'].__len__()}")

    config_dirs = [
        Path("/mnt/c/Users/星记/AppData/Roaming/JetBrains/PyCharm2026.2"),
    ]
    for directory in config_dirs:
        if not directory.is_dir():
            print(f"skip missing config dir: {directory}")
            continue
        written = install_sidecar(files, "10.9", [directory])
        for target in written:
            print(f"installed -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
