"""
The single entrypoint for the whole application.

    python main.py              launch the app window / server (editor, backtest, live)
    python main.py backtest     run a backtest headlessly (all backtest.py flags apply)
    python main.py live         run the live engine headlessly until interrupted
    python main.py doctor       print resolved paths and credential status
    python main.py credentials  prompt for each credential and store it in the OS keyring
    python main.py credentials --clear   delete every stored credential and the saved session

Every mode goes through the same startup: resolve directories, configure
logging, create default config files if this is a fresh install, migrate any
legacy .env credentials into the OS keyring.
"""

from __future__ import annotations

import logging
import signal
import sys
import time

import app_logging
import app_paths
import secrets_store
from ui import persistence

def bootstrap(to_console: bool = True) -> None:
    app_paths.ensure_dirs()
    app_logging.configure(level=logging.INFO, to_console=to_console)
    log = app_logging.get_logger(__name__)
    log.info("data directory: %s", app_paths.describe())
    persistence.ensure_default_configs()
    try:
        moved = secrets_store.migrate_env_file()
        if moved:
            log.info("moved %s out of .env into the keyring; the .env copies can be deleted",
                     ", ".join(moved))
    except Exception as error:
        log.warning("credential migration skipped: %s", error)

def run_doctor() -> int:
    print(f"base directory : {app_paths.describe()}")
    for name in ("config_dir", "data_dir", "cache_dir", "output_dir", "log_dir"):
        print(f"  {name:12}: {getattr(app_paths, name)()}")
    print(f"\nkeyring available: {secrets_store.keyring_available()}")
    print("credentials:")
    for key, present in secrets_store.credential_status().items():
        print(f"  {key:18}: {'set' if present else 'MISSING'}")
    session = secrets_store.read_session()
    print(f"\nsession: {'present' if session.get('KITE_ACCESS_TOKEN') else 'none'}"
          f" (last login {session.get('KITE_LOGIN_TIME', 'never')})")
    return 0

def run_credentials(clear: bool = False) -> int:
    import getpass

    if clear:
        answer = input("Type 'clear' to delete every stored credential and the saved session: ")
        if answer != "clear":
            print("cancelled")
            return 1
        for key in secrets_store.CREDENTIAL_KEYS:
            secrets_store.delete_credential(key)
        secrets_store.clear_session()
        print("cleared")
        return 0

    print("Enter each credential (leave blank to keep the current value).\n")
    status = secrets_store.credential_status()
    for key in secrets_store.CREDENTIAL_KEYS:
        value = getpass.getpass(f"{key} [{'set' if status[key] else 'MISSING'}]: ")
        if value:
            secrets_store.set_credential(key, value)

    print("\ncredentials:")
    for key, present in secrets_store.credential_status().items():
        print(f"  {key:18}: {'set' if present else 'MISSING'}")
    return 0

def run_live_headless() -> int:
    from live_service import SERVICE

    log = app_logging.get_logger(__name__)
    SERVICE.start()

    stopping = {"now": False}
    def handle(signum, frame):
        stopping["now"] = True
    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)

    try:
        while not stopping["now"] and SERVICE.state != "error":
            time.sleep(0.5)
    finally:
        SERVICE.stop()
    if SERVICE.error:
        log.error("live engine exited with error: %s", SERVICE.error)
        return 1
    return 0

def run_app() -> int:
    from nicegui import ui
    from ui.app import index  # noqa: F401  (registers the page)

    # A frozen build opens its own desktop window (native=True auto-picks a
    # free port); running from source keeps the plain browser-tab workflow,
    # since that's what every dev tool here drives.
    ui.run(title="Trading Signals", native=app_paths.is_frozen(), reload=False, show=False)
    return 0

def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "app"

    if mode == "backtest":
        sys.argv = sys.argv[:1] + sys.argv[2:]
        bootstrap()
        import backtest
        backtest.main()
        return 0

    if mode == "live":
        bootstrap()
        return run_live_headless()

    if mode == "doctor":
        bootstrap(to_console=False)
        return run_doctor()

    if mode == "credentials":
        bootstrap(to_console=False)
        return run_credentials(clear="--clear" in sys.argv[2:])

    if mode in ("app", "ui"):
        bootstrap()
        return run_app()

    print(__doc__)
    return 2

if __name__ in {"__main__", "__mp_main__"}:
    raise SystemExit(main())
