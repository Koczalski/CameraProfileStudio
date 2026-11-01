"""Standalone entry point: no arguments launches the GUI; arguments use the CLI."""
import sys

if __name__ == "__main__":
    if len(sys.argv) > 1:
        from camera_profile_studio.cli import main
    else:
        from camera_profile_studio.gui.app import main
    raise SystemExit(main())
