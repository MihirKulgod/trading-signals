"""Entrypoint for the config-editor UI. Run: kite-env/bin/python run_ui.py"""

from ui.app import main

if __name__ in {"__main__", "__mp_main__"}:
    main()
