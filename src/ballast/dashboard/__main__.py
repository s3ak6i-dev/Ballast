"""Entry point:  python -m ballast.dashboard  →  http://127.0.0.1:8080"""

import uvicorn

from .app import create_app


def main(host: str = "127.0.0.1", port: int = 8080) -> None:
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
