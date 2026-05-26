"""Seed default data files into the persistent volume on first boot.

The Railway volume mounts over /app/data, so on first deploy it's empty.
This script copies default files from /app/data_defaults into /app/data
only if they don't already exist, preserving any user customizations.
"""

import os
import shutil

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DEFAULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_defaults")


def seed():
    if not os.path.exists(DEFAULTS_DIR):
        print("seed_data: No defaults directory found, skipping.")
        return

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "reference_contracts"), exist_ok=True)

    # Files that ALWAYS get refreshed from the image. These are not user-edited
    # (the user customizes data/prompts.json via the admin UI, not these), so
    # they must reflect whatever ships with each deploy. Without this, updates
    # to the packaged defaults can never reach an existing install.
    ALWAYS_REFRESH = {"prompts_default.json"}

    seeded = 0
    refreshed = 0
    for root, dirs, files in os.walk(DEFAULTS_DIR):
        rel_root = os.path.relpath(root, DEFAULTS_DIR)
        dest_root = os.path.join(DATA_DIR, rel_root) if rel_root != "." else DATA_DIR

        os.makedirs(dest_root, exist_ok=True)

        for fname in files:
            src = os.path.join(root, fname)
            dest = os.path.join(dest_root, fname)
            if fname in ALWAYS_REFRESH:
                shutil.copy2(src, dest)
                refreshed += 1
                print(f"seed_data: Refreshed {os.path.join(rel_root, fname)}")
            elif not os.path.exists(dest):
                shutil.copy2(src, dest)
                seeded += 1
                print(f"seed_data: Copied {os.path.join(rel_root, fname)}")

    if refreshed:
        print(f"seed_data: Refreshed {refreshed} always-current file(s) from the image.")

    if seeded:
        print(f"seed_data: Seeded {seeded} default file(s) into data volume.")
    else:
        print("seed_data: All default files already present, nothing to seed.")


if __name__ == "__main__":
    seed()
