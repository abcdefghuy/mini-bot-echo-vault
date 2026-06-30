"""
scraper/converter.py - HTML to clean Markdown conversion.

Custom markdownify converter with better handling of images, links,
and noise removal for Zendesk support articles.
"""

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from markdownify import MarkdownConverter


# Tags / CSS selectors to strip from article HTML before conversion
NOISE_SELECTORS = [
    "nav", "header", "footer",
    ".header", ".footer", ".navigation", ".nav",
    ".breadcrumb", ".breadcrumbs",
    ".sidebar", ".side-bar",
    ".advertisement", ".ad", ".ads",
    ".cookie-banner", ".cookie-notice",
    ".social-share", ".share-buttons",
    ".related-articles", ".related-posts",
    ".comment-section", ".comments",
    ".announcement", ".banner",
    "script", "style", "noscript", "iframe",
    "[role='navigation']", "[role='banner']",
]


class CleanMarkdownConverter(MarkdownConverter):
    """Custom markdownify converter with better handling of images and links."""

    def convert_img(self, el, text, parent_tags):
        alt = el.get("alt", "")
        src = el.get("src", "")
        title = el.get("title", "")

        # Skip base64 inline images as they cause Gemini API ingestion failures
        if src and src.startswith("data:image/"):
            return "<!-- [Base64 Image Removed] -->"

        # Skip tracking pixels and tiny images
        width = el.get("width", "")
        height = el.get("height", "")
        if width and height:
            try:
                if int(width) <= 1 or int(height) <= 1:
                    return ""
            except ValueError:
                pass

        if title:
            return f"![{alt}]({src} \"{title}\")"
        return f"![{alt}]({src})"

    def convert_a(self, el, text, parent_tags):
        href = el.get("href", "")
        title = el.get("title", "")

        # Skip empty links or anchor-only links
        if not href or not text.strip():
            return text

        # Keep relative links as-is, convert absolute optisigns links to relative
        if href.startswith("https://support.optisigns.com"):
            parsed = urlparse(href)
            href = parsed.path
            if parsed.fragment:
                href += f"#{parsed.fragment}"

        if title:
            return f"[{text.strip()}]({href} \"{title}\")"
        return f"[{text.strip()}]({href})"


def html_to_clean_markdown(html_content: str, article_url: str = "") -> str:
    """
    Convert HTML content to clean Markdown.

    Steps:
    1. Parse with BeautifulSoup
    2. Remove noise elements (nav, ads, scripts, etc.)
    3. Convert to Markdown using markdownify
    4. Clean up whitespace and formatting
    """
    if not html_content:
        return ""

    soup = BeautifulSoup(html_content, "html.parser")

    # ---- Remove noise elements ----
    for selector in NOISE_SELECTORS:
        for element in soup.select(selector):
            element.decompose()

    # ---- Remove empty elements ----
    for tag in soup.find_all():
        if tag.name not in ("img", "br", "hr") and not tag.get_text(strip=True) and not tag.find("img"):
            tag.decompose()

    # ---- Convert to Markdown ----
    markdown_text = CleanMarkdownConverter(
        heading_style="atx",
        bullets="-",
        strong_em_symbol="**",
        code_language="",
        strip=["button"],
        wrap=False,
        wrap_width=0,
    ).convert(str(soup))

    # ---- Post-processing cleanup ----
    # Remove excessive blank lines (keep max 2)
    markdown_text = re.sub(r"\n{4,}", "\n\n\n", markdown_text)

    # Remove trailing whitespace on each line
    markdown_text = "\n".join(line.rstrip() for line in markdown_text.split("\n"))

    # Remove leading/trailing whitespace
    markdown_text = markdown_text.strip()

    return markdown_text
