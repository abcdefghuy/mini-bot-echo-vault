"""
utils/helpers.py - Shared utility functions.
"""

import re
import hashlib


def slugify(text: str) -> str:
    """Convert article title to a URL-friendly slug for filename."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)      # Remove non-alphanumeric
    text = re.sub(r"[\s_]+", "-", text)        # Spaces/underscores to hyphens
    text = re.sub(r"-{2,}", "-", text)         # Collapse multiple hyphens
    text = text.strip("-")
    return text[:80] if text else "untitled"    # Cap length


def compute_hash(content: str) -> str:
    """Compute SHA-256 hash of content for change detection."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
