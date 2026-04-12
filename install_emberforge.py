#!/usr/bin/env python3
"""
Ember Forge Installer
=====================
Download and run this file. That's it.

    python install_emberforge.py

It will:
  1. Download Ember Forge to ~/EmberForge/
  2. Install dependencies (anthropic SDK)
  3. Ask for your API key
  4. Create a desktop shortcut
  5. Launch the game

Requirements: Python 3.10+ (python.org)
"""

import os
import sys
import subprocess
import zipfile
import shutil
import io

REPO_ZIP = "https://github.com/kuhlmanne/EmberForge/archive/refs/heads/main.zip"
INSTALL_DIR = os.path.join(os.path.expanduser("~"), "EmberForge")
GAME_DIR = os.path.join(INSTALL_DIR, "game")

REQUIRED_FILES = [
    "studio_server.py", "session_forge.py", "session_store.py",
    "stress_store.py", "memento_forge.py", "memento_store.py",
    "persona_affinities.py", "persona_sheets.py", "camp_skills.py",
    "game_progression.py", "reward_cards.py", "llm_router.py",
    "game_voice.py", "voice_forge.py",
    "amplify_studio.html", "start.py", "forge.bat",
]


def banner():
    print()
    print("  " + "=" * 46)
    print("    EMBER FORGE — every voice carries weight")
    print("  " + "=" * 46)
    print()


def check_python():
    v = sys.version_info
    if v.major < 3 or (v.major == 3 and v.minor < 10):
        print(f"  Python 3.10+ required (you have {v.major}.{v.minor})")
        print(f"  Download from: https://python.org")
        sys.exit(1)
    print(f"  Python {v.major}.{v.minor}.{v.micro} ✓")


def install_deps():
    print("  Installing dependencies...")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "anthropic", "-q"],
        stdout=subprocess.DEVNULL,
    )
    print("  anthropic SDK ✓")


def download_game():
    if os.path.exists(GAME_DIR) and os.path.exists(os.path.join(GAME_DIR, "studio_server.py")):
        print(f"  Game already installed at {GAME_DIR}")
        resp = input("  Reinstall? (y/N): ").strip().lower()
        if resp != "y":
            return

    print(f"  Downloading Ember Forge...")
    import urllib.request
    try:
        with urllib.request.urlopen(REPO_ZIP, timeout=60) as r:
            data = r.read()
    except Exception as e:
        print(f"  Download failed: {e}")
        print(f"  You can manually download from: {REPO_ZIP}")
        sys.exit(1)

    print(f"  Extracting...")
    os.makedirs(INSTALL_DIR, exist_ok=True)

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        # Find the root folder name in the zip (usually "wingman-main/")
        roots = set()
        for name in zf.namelist():
            parts = name.split("/")
            if len(parts) > 1:
                roots.add(parts[0])
        zip_root = roots.pop() if len(roots) == 1 else "wingman-main"

        # Extract game files to GAME_DIR
        if os.path.exists(GAME_DIR):
            shutil.rmtree(GAME_DIR, ignore_errors=True)
        os.makedirs(GAME_DIR, exist_ok=True)

        for member in zf.namelist():
            if not member.startswith(zip_root + "/"):
                continue
            rel = member[len(zip_root) + 1:]
            if not rel:
                continue
            dest = os.path.join(GAME_DIR, rel)
            if member.endswith("/"):
                os.makedirs(dest, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(member) as src, open(dest, "wb") as dst:
                    dst.write(src.read())

    print(f"  Installed to {GAME_DIR} ✓")


def setup_api_key():
    env_path = os.path.join(GAME_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            if "ANTHROPIC_API_KEY" in f.read():
                print("  API key already configured ✓")
                return

    print()
    print("  To play Ember Forge, you need an Anthropic API key.")
    print("  Get one at: https://console.anthropic.com")
    print()
    key = input("  Paste your API key (sk-ant-...): ").strip()
    if not key:
        print("  No key entered. You can set it later in Settings.")
        return

    with open(env_path, "a") as f:
        f.write(f"\nANTHROPIC_API_KEY={key}\n")
    print("  API key saved ✓")


def create_shortcut():
    if sys.platform == "win32":
        try:
            desktop = os.path.join(os.path.expanduser("~"), "Desktop")
            shortcut_path = os.path.join(desktop, "Ember Forge.lnk")
            target = os.path.join(GAME_DIR, "forge.bat")

            ps_cmd = (
                f'$ws = New-Object -ComObject WScript.Shell; '
                f'$s = $ws.CreateShortcut("{shortcut_path}"); '
                f'$s.TargetPath = "{target}"; '
                f'$s.WorkingDirectory = "{GAME_DIR}"; '
                f'$s.Description = "Ember Forge"; '
                f'$s.Save()'
            )
            subprocess.run(["powershell", "-Command", ps_cmd],
                         capture_output=True, timeout=10)
            print(f"  Desktop shortcut created ✓")
        except Exception as e:
            print(f"  Shortcut creation failed: {e}")
    else:
        print(f"  To launch: cd {GAME_DIR} && python start.py")


def launch():
    print()
    resp = input("  Launch Ember Forge now? (Y/n): ").strip().lower()
    if resp == "n":
        print(f"\n  To play later: double-click 'Ember Forge' on your desktop")
        print(f"  Or: cd {GAME_DIR} && python start.py")
        return

    print("  Starting Ember Forge...")
    if sys.platform == "win32":
        subprocess.Popen(
            ["cmd", "/c", "forge.bat"],
            cwd=GAME_DIR,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    else:
        subprocess.Popen(
            [sys.executable, "start.py"],
            cwd=GAME_DIR,
        )


def main():
    banner()
    check_python()
    install_deps()
    download_game()
    setup_api_key()
    create_shortcut()
    launch()
    print()
    print("  ✓ Ember Forge is ready. Every voice carries weight.")
    print()


if __name__ == "__main__":
    main()
