"""Run with:  python -m saferoute   (HOST / PORT env vars optional)"""
import logging
import os

import uvicorn

from .app import create_app

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    uvicorn.run(create_app(), host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
