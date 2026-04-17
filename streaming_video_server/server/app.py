from __future__ import annotations

from aiohttp import web

from .config import ServerConfig
from .webrtc import make_app


def main() -> None:
    config = ServerConfig.from_env()
    app = web.run_app(make_app(config), host=config.host, port=config.port)
    return app


if __name__ == "__main__":
    main()
