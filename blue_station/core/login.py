"""Starting Blue Station at login, on macOS. No Qt.

A LaunchAgent opens a small app bundle when you log in, and the bundle
runs this Python with this Blue Station, in the menu bar. The bundle is
what lets macOS ask for Bluetooth on Blue Station's behalf: started by
launchd directly, Python would be refused Bluetooth."""
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "io.github.mrchillstorm.bluestation"
BLUETOOTH_WHY = "Blue Station listens for Bluetooth devices nearby to show them and their signal strength."


def supported() -> bool:
    return sys.platform == "darwin"


def agent_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def bundle_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "Application Support" / "Blue Station" / "Blue Station.app"


def enabled(home: Path | None = None) -> bool:
    return agent_path(home).exists()


def _repo_icon() -> Path | None:
    """The app icon, when running from a copy of the repository."""
    icon = Path(__file__).resolve().parents[2] / "Blue Station.app" / "Contents" / "Resources" / "AppIcon.icns"
    return icon if icon.exists() else None


def write_bundle(target: Path, python: str | None = None) -> None:
    """An app bundle whose only job is to run `python -m blue_station`."""
    code_dir = Path(__file__).resolve().parents[2]  # the folder that holds the blue_station package
    macos, resources = target / "Contents" / "MacOS", target / "Contents" / "Resources"
    macos.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)
    info = {
        "CFBundleName": "Blue Station", "CFBundleDisplayName": "Blue Station", "CFBundleIdentifier": LABEL,
        "CFBundleExecutable": "launch", "CFBundlePackageType": "APPL", "CFBundleIconFile": "AppIcon",
        "LSMinimumSystemVersion": "10.15", "NSBluetoothAlwaysUsageDescription": BLUETOOTH_WHY,
    }
    with (target / "Contents" / "Info.plist").open("wb") as f:
        plistlib.dump(info, f)
    script = macos / "launch"
    script.write_text(
        "#!/bin/sh\n"
        "# Written by Blue Station for starting at login. It runs the same Blue Station that wrote it.\n"
        f'export PYTHONPATH="{code_dir}${{PYTHONPATH:+:$PYTHONPATH}}"\n'
        f'exec "{python or sys.executable}" -m blue_station "$@"\n', encoding="utf-8")
    script.chmod(0o755)
    icon = _repo_icon()
    if icon is not None:
        shutil.copyfile(icon, resources / "AppIcon.icns")


def enable(home: Path | None = None, python: str | None = None) -> None:
    bundle = bundle_path(home)
    write_bundle(bundle, python)
    agent = agent_path(home)
    agent.parent.mkdir(parents=True, exist_ok=True)
    job = {
        "Label": LABEL,
        # -g: stay in the background; --background: straight to the menu bar, no window
        "ProgramArguments": ["/usr/bin/open", "-g", "-a", str(bundle), "--args", "--background"],
        "RunAtLoad": True,
        "LimitLoadToSessionType": "Aqua",
        "AssociatedBundleIdentifiers": [LABEL],  # System Settings shows Blue Station, not "open"
    }
    temp = agent.with_suffix(".tmp")
    with temp.open("wb") as f:
        plistlib.dump(job, f)
    temp.replace(agent)


def disable(home: Path | None = None) -> None:
    agent = agent_path(home)
    if agent.exists():
        agent.unlink()
        if home is None:  # a job loaded at this login would otherwise linger until logout
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    shutil.rmtree(bundle_path(home), ignore_errors=True)
