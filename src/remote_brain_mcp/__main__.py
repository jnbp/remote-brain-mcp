"""Entry point: `python -m remote_brain_mcp` or the `remote-brain-mcp` command."""

from __future__ import annotations

import logging
import sys
import threading

import uvicorn

from . import __version__
from .config import Config, ConfigError
from .server import build_app, start_background_jobs
from .vault import Vault


def main() -> None:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"remote-brain-mcp: configuration error: {exc}", file=sys.stderr)
        sys.exit(2)

    logging.basicConfig(
        level=config.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if config.log_level != "debug":
        logging.getLogger("mcp").setLevel(logging.WARNING)  # per-request noise from the SDK
    log = logging.getLogger("remote_brain_mcp")
    log.info("remote-brain-mcp %s starting, vault=%s", __version__, config.vault_path)
    if config.token_generated:
        log.warning("no RB_TOKEN set: created a new token in %s/token (shown only this once)", config.data_path)
        log.warning("token: %s", config.token)

    vault = Vault(config)
    vault.startup()
    log.info("index ready: %d notes", vault.index.note_count())
    vault.purge_trash()

    stop = threading.Event()
    start_background_jobs(vault, stop)
    app = build_app(config, vault)
    try:
        uvicorn.run(app, host=config.host, port=config.port, log_level=config.log_level,
                    server_header=False, access_log=False)
    finally:
        stop.set()


if __name__ == "__main__":
    main()
