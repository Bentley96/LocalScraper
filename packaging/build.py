"""Build the Site Collector desktop app with PyInstaller.

    python packaging/build.py            ->  dist/Site Collector.app (macOS) or dist/Site Collector/ (Windows/Linux)

Run it on the platform you are building for (a Mac app must be built on a Mac).
GitHub Actions does this automatically — see .github/workflows/build-app.yml.
"""
from __future__ import annotations

import platform
import shutil
import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parents[1]
NAME = "Site Collector"


def main() -> None:
    icon = ROOT / "packaging" / "icon.png"
    if not icon.exists():
        import runpy
        runpy.run_path(str(ROOT / "packaging" / "make_icon.py"))
    args = [
        str(ROOT / "app.py"),
        "--name", NAME,
        "--windowed",
        "--noconfirm",
        "--clean",
        "--icon", str(icon),
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        # Playwright has no PyInstaller hook: bundle its Node driver so "Render in Chrome" works.
        "--collect-all", "playwright",
        "--collect-data", "certifi",
    ]
    if platform.system() == "Darwin":
        args += ["--osx-bundle-identifier", "com.localscraper.sitecollector"]
    PyInstaller.__main__.run(args)
    shutil.rmtree(ROOT / "build", ignore_errors=True)
    print(f"\nBuilt: {ROOT / 'dist'}", file=sys.stderr)


if __name__ == "__main__":
    main()
