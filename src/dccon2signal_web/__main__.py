import os

import uvicorn


def run() -> None:
    uvicorn.run(
        "dccon2signal_web.app:app",
        host=os.environ.get("DCCON2SIGNAL_WEB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DCCON2SIGNAL_WEB_PORT", "8000")),
    )


if __name__ == "__main__":
    run()
