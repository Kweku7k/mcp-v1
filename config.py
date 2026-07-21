import os

from dotenv import load_dotenv

load_dotenv()


PRESTOQ_API_URL = os.getenv("PRESTOQ_API_URL", "").rstrip("/")
PRESTOQ_API_TOKEN = os.getenv("PRESTOQ_API_TOKEN") or os.getenv("PRESTO_API_TOKEN", "")


if not PRESTOQ_API_URL:
    raise RuntimeError("PRESTOQ_API_URL is not configured")