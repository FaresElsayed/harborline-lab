"""Build setup/harborline-chat.zip: only what chat colleagues may read, so the answer key can't reach a chat.

Python's zipfile always writes forward slashes, which an AI site's Linux sandbox needs. Windows PowerShell 5.1's
Compress-Archive may not, so this is built ahead of time and the setup files only copy it.

    python -m harborline.tools.build_chat_zip
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[3]
OUTPUT = Path("setup") / "harborline-chat.zip"
INCLUDE = ("START_HERE.html", "handbook", "tracker", "brand", "team/iris-review")
# team.html holds every colleague's prompt, which would let one read another's knowledge.
EXCLUDE_NAMES = {"answer-key.html", "team.html", ".DS_Store", "Thumbs.db"}
STAMP = (2026, 1, 1, 0, 0, 0)


def members(root: Path) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    for item in INCLUDE:
        path = root / item
        files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        found += [(f.relative_to(root).as_posix(), f) for f in files if f.name not in EXCLUDE_NAMES]
    return found


def build(root: Path = DEFAULT_ROOT) -> bytes:
    """Same input, same bytes: fixed timestamps and a fixed order."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, path in members(root):
            info = zipfile.ZipInfo(f"harborline-lab/{name}", STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return buffer.getvalue()


def main() -> None:
    out = DEFAULT_ROOT / OUTPUT
    out.write_bytes(build())
    print(f"wrote {out} ({out.stat().st_size / 1_048_576:.1f} MB)")


if __name__ == "__main__":
    main()
