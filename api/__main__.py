"""``python -m api``: run the service with uvicorn (host/port from T2S_HOST / T2S_PORT)."""
from __future__ import annotations

import logging
import os


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=os.getenv("T2S_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s",
    )
    # Importing the app installs the log record factory that provides %(request_id)s.
    from api.main import app

    uvicorn.run(
        app,
        host=os.getenv("T2S_HOST", "127.0.0.1"),
        port=int(os.getenv("T2S_PORT", "8000")),
        log_config=None,
    )


if __name__ == "__main__":
    main()
