"""
Build script for Ember Forge standalone exe.
Run: python build_forge.py

Creates dist/EmberForge.exe — a single file that contains:
  - Python runtime
  - Studio server + all CRPG modules
  - amplify_studio.html
  - Persona definitions
  - anthropic SDK

Player downloads one file, double-clicks, plays. No Python install needed.
"""
import PyInstaller.__main__
import os
import shutil

WINGMAN_DIR = os.path.dirname(os.path.abspath(__file__))

# Files to bundle as data
DATA_FILES = [
    ('amplify_studio.html', '.'),
    ('personas', 'personas'),
    ('persona_images', 'persona_images'),
    ('EMBER_FORGE_README.md', '.'),
]

# All Python modules the server imports
HIDDEN_IMPORTS = [
    'session_forge', 'session_store', 'stress_store',
    'memento_forge', 'memento_store', 'persona_affinities',
    'persona_sheets', 'camp_skills', 'game_progression',
    'reward_cards', 'llm_router', 'game_voice', 'voice_forge',
    'persona_registry', 'interaction_store',
    'anthropic', 'json', 'threading', 'http.server',
]

# Build the --add-data args
add_data_args = []
for src, dst in DATA_FILES:
    src_path = os.path.join(WINGMAN_DIR, src)
    if os.path.exists(src_path):
        add_data_args.extend(['--add-data', f'{src_path}{os.pathsep}{dst}'])

# Build the --hidden-import args
hidden_args = []
for mod in HIDDEN_IMPORTS:
    hidden_args.extend(['--hidden-import', mod])

print("Building Ember Forge executable...")
print(f"  Source: {WINGMAN_DIR}")

PyInstaller.__main__.run([
    os.path.join(WINGMAN_DIR, 'start.py'),
    '--name', 'EmberForge',
    '--onefile',
    '--console',  # needs console for the "press any key to stop" behavior
    '--icon', 'NONE',
    *add_data_args,
    *hidden_args,
    '--distpath', os.path.join(WINGMAN_DIR, 'dist'),
    '--workpath', os.path.join(WINGMAN_DIR, 'build_temp'),
    '--specpath', os.path.join(WINGMAN_DIR, 'build_temp'),
    '--clean',
    '--noconfirm',
])

# Check output
exe_path = os.path.join(WINGMAN_DIR, 'dist', 'EmberForge.exe')
if os.path.exists(exe_path):
    size_mb = os.path.getsize(exe_path) / (1024 * 1024)
    print(f"\n  ✓ Built: {exe_path}")
    print(f"  ✓ Size: {size_mb:.1f} MB")
    print(f"\n  Share this one file. Double-click to play.")
else:
    print("\n  ✗ Build failed. Check output above.")

# Cleanup
build_temp = os.path.join(WINGMAN_DIR, 'build_temp')
if os.path.exists(build_temp):
    shutil.rmtree(build_temp, ignore_errors=True)
