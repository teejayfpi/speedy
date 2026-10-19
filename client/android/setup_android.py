#!/usr/bin/env python3
"""Post-process the generated Capacitor Android project.

Capacitor creates android/ from a template at build time, so the changes Speedy
needs cannot simply be committed. Run this after `npx cap add android` (or
`npx cap sync android`) to:

  1. allow cleartext HTTP, because a Speedy server on your LAN is plain
     http:// and Android blocks cleartext by default;
  2. install the Speedy launcher icons and splash colours.

Idempotent: safe to run repeatedly.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent          # repository root
ANDROID = HERE / "android"         # generated Capacitor project
RES = ANDROID / "app" / "src" / "main" / "res"
MANIFEST = ANDROID / "app" / "src" / "main" / "AndroidManifest.xml"
ICON_SRC = ROOT / "assets" / "android"

# Density folder -> launcher icon files shipped in assets/android.
ICON_FILES = ("ic_launcher.png", "ic_launcher_foreground.png",
              "ic_launcher_background.png")


def fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def patch_manifest() -> None:
    if not MANIFEST.is_file():
        fail(f"AndroidManifest.xml not found at {MANIFEST}; run "
             "`npx cap add android` first")
    text = MANIFEST.read_text(encoding="utf-8")

    if 'android:usesCleartextTraffic="true"' in text:
        print("manifest: cleartext already enabled")
        return

    # Add the attribute to the <application ...> start tag.
    new, n = re.subn(
        r"(<application\b)",
        r'\1 android:usesCleartextTraffic="true"',
        text,
        count=1,
    )
    if n != 1:
        fail("could not find the <application> tag in AndroidManifest.xml")
    MANIFEST.write_text(new, encoding="utf-8")
    print("manifest: cleartext HTTP enabled for LAN servers")


def install_icons() -> None:
    if not ICON_SRC.is_dir():
        fail(f"no launcher icons at {ICON_SRC}")
    copied = 0
    for density in sorted(ICON_SRC.iterdir()):
        if not density.is_dir():
            continue
        dest = RES / density.name
        dest.mkdir(parents=True, exist_ok=True)
        for name in ICON_FILES:
            src = density / name
            if src.is_file():
                shutil.copy2(src, dest / name)
                copied += 1
    if not copied:
        fail("no launcher icons were copied")
    print(f"icons: installed {copied} files into {RES.relative_to(ROOT)}")


def main() -> int:
    patch_manifest()
    install_icons()
    print("android project ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
