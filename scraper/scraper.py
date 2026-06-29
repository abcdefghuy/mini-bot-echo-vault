"""
scraper/scraper.py - OptiSigns support article scraper.

Orchestrates fetching articles from Zendesk, converting to Markdown,
and saving with delta detection via content hashing.
"""

import os
import json
import time
import logging
from pathlib import Path

from .api import ZendeskClient
from .converter import html_to_clean_markdown
from utils import slugify, compute_hash

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)


class OptiSignsScraper:
    """
    Scrapes OptiSigns support articles via Zendesk Help Center API
    and converts them to clean Markdown files.

    Supports delta detection via content hashing to avoid re-processing
    unchanged articles.
    """

    def __init__(
        self,
        output_dir: str = None,
        hash_store_file: str = None,
        base_url: str = None,
    ):
        self.base_url = base_url or os.getenv("SUPPORT_BASE_URL", "https://support.optisigns.com")
        self.output_dir = Path(output_dir or os.getenv("OUTPUT_DIR", "articles"))
        self.hash_store_file = Path(hash_store_file or os.getenv("HASH_STORE_FILE", "article_hashes.json"))
        self.api_client = ZendeskClient(self.base_url)

        # Stats
        self.stats = {"added": 0, "updated": 0, "skipped": 0, "errors": 0, "total_fetched": 0}

    # ------------------------------------------------------------------
    # Hash store: load / save
    # ------------------------------------------------------------------
    def _load_hashes(self) -> dict:
        """Load previously stored article hashes."""
        if self.hash_store_file.exists():
            with open(self.hash_store_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save_hashes(self, hashes: dict):
        """Persist article hashes to disk."""
        with open(self.hash_store_file, "w", encoding="utf-8") as f:
            json.dump(hashes, f, indent=2, ensure_ascii=False)

    # ------------------------------------------------------------------
    # Article processing
    # ------------------------------------------------------------------
    def _build_article_header(self, article: dict) -> str:
        """Build a YAML-like metadata header and title for the Markdown file."""
        title = article.get("title", "Untitled")
        article_url = article.get("html_url", "")
        updated_at = article.get("updated_at", "")
        article_id = article.get("id", "")

        header = f"# {title}\n\n"
        header += f"> **Source**: {article_url}  \n"
        header += f"> **Last Updated**: {updated_at}  \n"
        header += f"> **Article ID**: {article_id}\n\n"
        header += "---\n\n"
        return header

    def _process_article(self, article: dict, existing_hashes: dict) -> tuple[str, str, bool]:
        """
        Process a single article: convert to Markdown, detect changes.

        Returns:
            (slug, markdown_content, is_changed, content_hash, article_id)
        """
        title = article.get("title", "Untitled")
        article_id = str(article.get("id", ""))
        html_body = self.api_client.fetch_article_body(article)

        if not html_body:
            logger.warning(f"Empty body for article: {title}")
            return None, None, False

        # Convert HTML to Markdown
        article_url = article.get("html_url", "")
        markdown_body = html_to_clean_markdown(html_body, article_url)
        header = self._build_article_header(article)
        full_markdown = header + markdown_body

        # Compute hash for delta detection
        new_hash = compute_hash(full_markdown)
        old_hash = existing_hashes.get(article_id, {}).get("hash", "")

        # Generate slug
        slug = slugify(title)

        is_changed = new_hash != old_hash
        return slug, full_markdown, is_changed, new_hash, article_id

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    def run(self) -> dict:
        """
        Run the full scraping pipeline:
        1. Fetch all articles from Zendesk API
        2. Convert to Markdown
        3. Detect changes (delta)
        4. Save only new/updated articles
        5. Return stats

        Returns:
            dict with counts: added, updated, skipped, errors, total_fetched
        """
        logger.info("=" * 60)
        logger.info("OptiSigns Support Scraper - Starting")
        logger.info("=" * 60)

        # Create output directory
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Load existing hashes
        existing_hashes = self._load_hashes()
        new_hashes = {}

        # Fetch all articles
        articles = self.api_client.fetch_all_articles()
        self.stats["total_fetched"] = len(articles)

        if not articles:
            logger.warning("No articles fetched. Exiting.")
            return self.stats

        # Process each article
        for i, article in enumerate(articles, 1):
            title = article.get("title", "Untitled")
            logger.info(f"[{i}/{len(articles)}] Processing: {title}")

            try:
                result = self._process_article(article, existing_hashes)
                if result[0] is None:
                    self.stats["errors"] += 1
                    continue

                slug, full_markdown, is_changed, content_hash, article_id = result

                # Store hash
                new_hashes[article_id] = {
                    "hash": content_hash,
                    "slug": slug,
                    "title": title,
                    "updated_at": article.get("updated_at", ""),
                }

                # Save file only if changed
                file_path = self.output_dir / f"{slug}.md"

                if article_id not in existing_hashes:
                    # New article
                    file_path.write_text(full_markdown, encoding="utf-8")
                    self.stats["added"] += 1
                    logger.info(f"  ✅ ADDED: {file_path.name}")
                elif is_changed:
                    # Updated article
                    file_path.write_text(full_markdown, encoding="utf-8")
                    self.stats["updated"] += 1
                    logger.info(f"  🔄 UPDATED: {file_path.name}")
                else:
                    # Unchanged
                    self.stats["skipped"] += 1
                    logger.info(f"  ⏭️  SKIPPED (unchanged): {file_path.name}")

                # Small delay to be nice to the API
                time.sleep(0.2)

            except Exception as e:
                logger.error(f"  ❌ ERROR processing '{title}': {e}")
                self.stats["errors"] += 1

        # Save updated hashes
        self._save_hashes(new_hashes)

        # Print summary
        logger.info("")
        logger.info("=" * 60)
        logger.info("SCRAPING COMPLETE - Summary")
        logger.info("=" * 60)
        logger.info(f"  Total fetched:  {self.stats['total_fetched']}")
        logger.info(f"  Added (new):    {self.stats['added']}")
        logger.info(f"  Updated:        {self.stats['updated']}")
        logger.info(f"  Skipped:        {self.stats['skipped']}")
        logger.info(f"  Errors:         {self.stats['errors']}")
        logger.info(f"  Output dir:     {self.output_dir.resolve()}")
        logger.info(f"  Hash store:     {self.hash_store_file.resolve()}")
        logger.info("=" * 60)

        return self.stats
