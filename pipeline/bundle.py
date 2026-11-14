"""The civic bundle: box 1 builds and signs it nightly; box 2 verifies it before loading anything.

Bundle = a directory of public data files plus MANIFEST.json (sha256 of every file, build time) and
MANIFEST.sig (Ed25519 signature over the manifest bytes). Box 1 holds the private key; box 2 holds
only the public key. Box 2 refuses a bundle if the signature fails, any file's hash differs, or a
file is present that the manifest doesn't list. A compromised box 1 job without the key can't slip
poisoned resources into the private zone.

    python -m pipeline.bundle keygen --out keys/                       # once; box 1 keeps bundle.key
    python -m pipeline.bundle build --key keys/bundle.key --out bundle/
    python -m pipeline.bundle verify --pub keys/bundle.pub bundle/     # box 2; exit 1 on any problem
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from careplan import ROOT

FILES = ["tracts.csv", "resources.csv", "dialysis_chicago.csv", "transit_wed.npz", "SOURCES.json"]


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def keygen(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    k = Ed25519PrivateKey.generate()
    (out / "bundle.key").write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                     serialization.NoEncryption()))
    (out / "bundle.key").chmod(0o600)
    (out / "bundle.pub").write_bytes(k.public_key().public_bytes(serialization.Encoding.PEM,
                                                                 serialization.PublicFormat.SubjectPublicKeyInfo))


def build(key: Path, out: Path, src: Path = ROOT / "data" / "processed", files: list[str] = FILES) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    entries = {}
    for name in files:
        if (src / name).exists():
            shutil.copy2(src / name, out / name)
            entries[name] = _sha(out / name)
    manifest = json.dumps({"built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "files": entries},
                          indent=2, sort_keys=True).encode()
    (out / "MANIFEST.json").write_bytes(manifest)
    sk = serialization.load_pem_private_key(key.read_bytes(), password=None)
    (out / "MANIFEST.sig").write_bytes(sk.sign(manifest))
    return out


def verify(pub: Path, bundle: Path) -> list[str]:
    """Returns problems; empty means the bundle is authentic and complete."""
    try:
        manifest = (bundle / "MANIFEST.json").read_bytes()
        sig = (bundle / "MANIFEST.sig").read_bytes()
    except FileNotFoundError as e:
        return [f"missing {e.filename}"]
    pk = serialization.load_pem_public_key(pub.read_bytes())
    assert isinstance(pk, Ed25519PublicKey)
    try:
        pk.verify(sig, manifest)
    except InvalidSignature:
        return ["signature does not match the manifest"]
    files = json.loads(manifest)["files"]
    problems = [f"hash mismatch: {n}" for n, h in files.items() if not (bundle / n).exists() or _sha(bundle / n) != h]
    extra = {p.name for p in bundle.iterdir()} - set(files) - {"MANIFEST.json", "MANIFEST.sig"}
    problems += [f"unlisted file: {n}" for n in sorted(extra)]
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen")
    k.add_argument("--out", type=Path, default=ROOT / "keys")
    b = sub.add_parser("build")
    b.add_argument("--key", type=Path, required=True)
    b.add_argument("--out", type=Path, default=ROOT / "bundle")
    v = sub.add_parser("verify")
    v.add_argument("--pub", type=Path, required=True)
    v.add_argument("bundle", type=Path)
    a = ap.parse_args(argv)
    if a.cmd == "keygen":
        keygen(a.out)
        print(f"keys in {a.out} (bundle.key stays on box 1)")
    elif a.cmd == "build":
        print(f"signed bundle at {build(a.key, a.out)}")
    else:
        problems = verify(a.pub, a.bundle)
        print("bundle OK" if not problems else "REFUSED:\n  " + "\n  ".join(problems))
        return 1 if problems else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
