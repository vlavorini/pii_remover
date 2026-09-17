#!/usr/bin/env python3
"""Rotate the encryption key material of already-written artifacts.

When ENCRYPTION_KEY changes (or a job ran before the key existed), the encrypted
files in data/outputs/ can no longer be read. This utility re-encrypts them with
the current key, given the old key.

    # artifacts encrypted with a previous key (any order; wrong keys are reported)
    python scripts/rotate_key.py --old-key-file old.key --old-key-file other.key

    # artifacts that are unreadable and must be discarded instead of migrated
    python scripts/rotate_key.py --old-key-file old.key --purge-unreadable
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cryptography.fernet import Fernet, InvalidToken  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.crypto import ArtifactCipher  # noqa: E402
from app.processing.pipeline import render_report  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--old-key", action="append", default=[],
                        help="a previous Fernet key (repeatable)")
    parser.add_argument("--old-key-file", action="append", default=[],
                        help="file containing a previous Fernet key (repeatable)")
    parser.add_argument("--outputs-dir", default=None,
                        help="directory holding the .enc artifacts")
    parser.add_argument("--purge-unreadable", action="store_true",
                        help="delete artifacts that no supplied key can open")
    parser.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    return parser.parse_args()


def load_old_fernets(args: argparse.Namespace) -> list[tuple[str, Fernet]]:
    found: list[tuple[str, Fernet]] = []
    for key in args.old_key:
        found.append(("--old-key", Fernet(key.strip().encode())))
    for path_str in args.old_key_file:
        raw = Path(path_str).read_text().strip()
        for line in raw.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                found.append((path_str, Fernet(line.encode())))
    return found


def main() -> int:
    args = parse_args()
    settings = get_settings()
    outputs = Path(args.outputs_dir) if args.outputs_dir else settings.app.output_dir

    if not settings.privacy.encryption_key:
        print("ENCRYPTION_KEY is not set: nothing to rotate TO.", file=sys.stderr)
        print("Set it in .env (python -m app.core.crypto) and retry.", file=sys.stderr)
        return 2

    target = ArtifactCipher(settings.privacy.encryption_key, enabled=True)
    old = load_old_fernets(args)
    if not old:
        print("No --old-key/--old-key-file supplied: every artifact will be reported "
              "as unreadable.", file=sys.stderr)

    artifacts = sorted(outputs.glob("*.enc"))
    migrated = failed = purged = skipped = 0

    for path in artifacts:
        blob = path.read_bytes()
        plain: bytes | None = None
        try:
            plain = target.decrypt_bytes(blob)
            skipped += 1
            continue  # already readable with the current key
        except Exception:  # noqa: BLE001
            pass

        for label, fernet in old:
            try:
                plain = fernet.decrypt(blob)
                break
            except InvalidToken:
                continue
            except Exception as exc:  # noqa: BLE001
                print(f"  ! {label}: {exc}")
                continue

        if plain is None:
            if args.purge_unreadable:
                if args.dry_run:
                    print(f"  would purge {path.name}")
                else:
                    path.unlink()
                    print(f"  purged {path.name} (unreadable)")
                purged += 1
            else:
                print(f"  UNREADABLE {path.name} (no supplied key opens it)")
                failed += 1
            continue

        if args.dry_run:
            print(f"  would re-encrypt {path.name}")
            migrated += 1
            continue

        path.write_bytes(target.encrypt_bytes(plain))
        print(f"  re-encrypted {path.name}")
        migrated += 1

    print(f"\nre-encrypted: {migrated} | already current: {skipped} | "
          f"unreadable: {failed} | purged: {purged}")
    if failed and not args.purge_unreadable:
        print("Re-run with --purge-unreadable to discard the unreadable artifacts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
