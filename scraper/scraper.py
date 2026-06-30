import os
import json
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from .api import ZendeskClient
from .converter import html_to_clean_markdown
from utils import slugify, compute_hash

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
MAX_WORKERS_SCRAPE = 10  # Concurrent threads for article processing


class OptiSignsScraper:

    def __init__(
        self,
        output_dir: str = None,
        hash_store_file: str = None,
        base_url: str = None,
        max_workers: int = MAX_WORKERS_SCRAPE,
    ):
        self.base_url = base_url or os.getenv("SUPPORT_BASE_URL", "https://support.optisigns.com")
        self.output_dir = Path(output_dir or os.getenv("OUTPUT_DIR", "articles"))
        self.hash_store_file = Path(hash_store_file or os.getenv("HASH_STORE_FILE", "article_hashes.json"))
        self.api_client = ZendeskClient(self.base_url)
        self.max_workers = max_workers

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

    def _process_single_article(
        self, article: dict, existing_hashes: dict, index: int, total: int
    ) -> dict:
        title = article.get("title", "Untitled")
        article_id = str(article.get("id", ""))

        try:
            html_body = self.api_client.fetch_article_body(article)

            if not html_body:
                logger.warning(f"[{index}/{total}] Empty body: {title}")
                return {"status": "error", "title": title}

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
            file_path = self.output_dir / f"{slug}.md"

            # Determine action
            is_new = article_id not in existing_hashes
            is_changed = new_hash != old_hash

            if is_new:
                file_path.write_text(full_markdown, encoding="utf-8")
                logger.info(f"[{index}/{total}] ADDED: {file_path.name}")
                status = "added"
            elif is_changed:
                file_path.write_text(full_markdown, encoding="utf-8")
                logger.info(f"[{index}/{total}] UPDATED: {file_path.name}")
                status = "updated"
            else:
                logger.info(f"[{index}/{total}] SKIPPED: {file_path.name}")
                status = "skipped"

            return {
                "status": status,
                "article_id": article_id,
                "hash": new_hash,
                "slug": slug,
                "title": title,
                "updated_at": article.get("updated_at", ""),
            }

        except Exception as e:
            logger.error(f"[{index}/{total}] ERROR '{title}': {e}")
            return {"status": "error", "title": title}

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    def run(self) -> dict:
        logger.info("=" * 60)
        logger.info("OptiSigns Support Scraper - Starting")
        logger.info(f"  Concurrency: {self.max_workers} threads")
        logger.info("=" * 60)

        # Create output directory
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Load existing hashes
        existing_hashes = self._load_hashes()
        new_hashes = {}

        # Fetch all articles (sequential — respects Zendesk rate limits)
        articles = self.api_client.fetch_all_articles()
        self.stats["total_fetched"] = len(articles)

        if not articles:
            logger.warning("No articles fetched. Exiting.")
            return self.stats

        logger.info(f"Processing {len(articles)} articles with {self.max_workers} threads...")

        # Process articles CONCURRENTLY
        futures = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            for i, article in enumerate(articles, 1):
                future = executor.submit(
                    self._process_single_article,
                    article, existing_hashes, i, len(articles)
                )
                futures[future] = article

            # Collect results as they complete
            for future in as_completed(futures):
                result = future.result()
                status = result.get("status", "error")

                if status == "added":
                    self.stats["added"] += 1
                elif status == "updated":
                    self.stats["updated"] += 1
                elif status == "skipped":
                    self.stats["skipped"] += 1
                else:
                    self.stats["errors"] += 1
                    continue

                # Store hash for successful processing
                article_id = result.get("article_id")
                if article_id:
                    new_hashes[article_id] = {
                        "hash": result["hash"],
                        "slug": result["slug"],
                        "title": result["title"],
                        "updated_at": result["updated_at"],
                    }

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
        logger.info(f"  Threads used:   {self.max_workers}")
        logger.info("=" * 60)

        return self.stats
