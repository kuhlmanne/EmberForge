#!/usr/bin/env python3
"""
AMPLIFY — standalone launcher.

Run this to start the game:
    python start.py

It will:
  1. Check Python version (3.10+)
  2. Install the anthropic SDK if missing
  3. Start the studio server
  4. Open your browser to the game
"""
import os
import sys
import subprocess
import webbrowser
import time

WINGMAN_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 7070


def check_python():
    v = sys.version_info
    if v.major < 3 or (v.major == 3 and v.minor < 10):
        print(f"Python 3.10+ required (you have {v.major}.{v.minor}.{v.micro})")
        sys.exit(1)
    print(f"Python {v.major}.{v.minor}.{v.micro}")


def check_deps():
    try:
        import anthropic  # noqa: F401
        print("anthropic SDK: installed")
    except ImportError:
        print("Installing anthropic SDK...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "anthropic", "-q"])
        print("anthropic SDK: installed")


def check_port():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("127.0.0.1", PORT)) == 0:
            print(f"Port {PORT} already in use - studio may already be running.")
            print(f"Open http://localhost:{PORT}/studio in your browser.")
            webbrowser.open(f"http://localhost:{PORT}/studio")
            sys.exit(0)


def start_server():
    print(f"Starting Amplify on port {PORT}...")
    # Start the server as a subprocess
    proc = subprocess.Popen(
        [sys.executable, "studio_server.py"],
        cwd=WINGMAN_DIR,
        stdout=open(os.path.join(WINGMAN_DIR, "studio_server.log"), "a"),
        stderr=subprocess.STDOUT,
    )
    print(f"Server PID: {proc.pid}")
    # Wait for it to be ready
    import socket
    for _ in range(30):
        time.sleep(0.5)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", PORT)) == 0:
                print(f"Amplify is live at http://localhost:{PORT}/studio")
                return proc
    print("Server failed to start. Check studio_server.log")
    sys.exit(1)


def main():
    print("=" * 50)
    print("  AMPLIFY — every voice carries weight")
    print("=" * 50)
    print()
    check_python()
    check_deps()
    check_port()
    proc = start_server()
    webbrowser.open(f"http://localhost:{PORT}/studio")
    print()
    print("Press Ctrl+C to stop the server.")
    print()
    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\nShutting down...")
        proc.terminate()
        proc.wait(timeout=5)
        print("Done.")


if __name__ == "__main__":
    main()
