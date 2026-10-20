#!/usr/bin/env python3
"""Build a deterministic, dependency-pinned Neon Function ZIP; never loads credentials."""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "services" / "continuity_identity"
OUTPUT = ROOT / ".tmp" / "continuity-identity.zip"

def build() -> dict:
    package = json.loads((SERVICE / "package-lock.json").read_text())
    expected = package["packages"]["node_modules/jose"]
    installed = json.loads((SERVICE / "node_modules/jose/package.json").read_text())
    if expected["version"] != "6.2.13" or installed["version"] != expected["version"]:
        raise RuntimeError("dependency_pin_mismatch")
    candidates = [SERVICE / "index.mjs", SERVICE / "package.json",
                  SERVICE / "node_modules/jose/package.json",
                  SERVICE / "node_modules/jose/LICENSE.md"]
    candidates += sorted((SERVICE / "node_modules/jose/dist").rglob("*.js"))
    OUTPUT.parent.mkdir(exist_ok=True, mode=0o700)
    with zipfile.ZipFile(OUTPUT, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(candidates):
            if not path.is_file() or path.is_symlink():
                raise RuntimeError("bundle_source_invalid")
            name = path.relative_to(SERVICE).as_posix()
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 20, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes(), compresslevel=9)
    os.chmod(OUTPUT, 0o600)
    payload = OUTPUT.read_bytes()
    return {"result": "IDENTITY_BUNDLE_BUILT", "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "chunks": (len(base64.b64encode(payload)) + 5999) // 6000}

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunk", type=int)
    args = parser.parse_args()
    if args.chunk is None:
        print(json.dumps(build(), sort_keys=True))
    else:
        encoded = base64.b64encode(OUTPUT.read_bytes()).decode("ascii")
        count = (len(encoded) + 5999) // 6000
        if args.chunk < 0 or args.chunk >= count:
            raise RuntimeError("bundle_chunk_invalid")
        print(json.dumps({"index": args.chunk, "count": count,
                          "data": encoded[args.chunk * 6000:(args.chunk + 1) * 6000]}))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
