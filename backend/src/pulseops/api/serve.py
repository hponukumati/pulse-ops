"""Production entrypoint: uvicorn with allowlisted logging for every logger (incl. uvicorn's)."""

import uvicorn

from pulseops.logging import uvicorn_log_config


def main() -> None:
    uvicorn.run(
        "pulseops.api.main:app",
        host="0.0.0.0",  # noqa: S104 - container-internal; only nginx is published (127.0.0.1)
        port=8000,
        log_config=uvicorn_log_config(),
        access_log=False,
        proxy_headers=False,  # forwarding headers are evaluated by the app, only from trusted proxies
        ws_max_size=16 * 1024,
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":
    main()
