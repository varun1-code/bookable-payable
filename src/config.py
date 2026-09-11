"""Environment/configuration for the autodraft pipeline."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

HIVE_API_KEY = os.environ.get("HIVE_API_KEY", "")
HIVE_BASE_URL = os.environ.get("HIVE_BASE_URL", "https://api.thehive.ai/api/v3/")
HIVE_VISION_MODEL = os.environ.get("HIVE_VISION_MODEL", "hive/vision-language-model")

DOCUMENTS_DIR = ROOT / "documents"
OUTPUT_DIR = ROOT / "output"
MASTER_DATA_DIR = ROOT / "master_data"
RENDER_CACHE_DIR = ROOT / ".render_cache"

# Page render resolution. Kept modest on purpose: these are mostly printed /
# rendered documents (not handwriting), and every extra pixel is billed as
# input tokens by the vision model. 150 DPI is legible for 8-11pt invoice text.
RENDER_DPI = int(os.environ.get("RENDER_DPI", "200"))
MAX_PAGE_LONG_EDGE = int(os.environ.get("MAX_PAGE_LONG_EDGE", "2000"))

# Safety valve for pathologically long PDFs (bundled multi-document dumps).
MAX_PAGES_PER_CALL = int(os.environ.get("MAX_PAGES_PER_CALL", "20"))
