"""Render PDF pages to base64 PNG data URIs for the vision model."""
from __future__ import annotations

import base64
import io
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image

from . import config


def render_pdf_pages(pdf_path: Path) -> list[str]:
    """Return a list of data:image/png;base64,... URIs, one per page."""
    uris = []
    doc = fitz.open(pdf_path)
    try:
        zoom = config.RENDER_DPI / 72.0
        mat = fitz.Matrix(zoom, zoom)
        for page in doc:
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            img = _shrink_to_max_edge(img, config.MAX_PAGE_LONG_EDGE)
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            uris.append(f"data:image/png;base64,{b64}")
    finally:
        doc.close()
    return uris


def _shrink_to_max_edge(img: Image.Image, max_edge: int) -> Image.Image:
    w, h = img.size
    long_edge = max(w, h)
    if long_edge <= max_edge:
        return img
    scale = max_edge / long_edge
    return img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
