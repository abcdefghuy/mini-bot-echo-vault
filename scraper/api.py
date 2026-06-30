"""
scraper/api.py - Zendesk Help Center API client.

Handles fetching articles from the Zendesk Help Center API
with pagination and rate limiting.
"""

import time
import logging

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_LOCALE = "en-us"
PER_PAGE = 100  # Zendesk max per page
RATE_LIMIT_PAUSE = 0.3  # seconds between API calls (Zendesk allows 200 req/min)

logger = logging.getLogger(__name__)


class ZendeskClient:
    """Fetches articles from Zendesk Help Center API with pagination."""

    def __init__(self, base_url: str):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "EchoVault-Scraper/1.0",
        })

    def fetch_all_articles(self) -> list[dict]:
        """
        Fetch all published articles from Zendesk Help Center API.
        Handles pagination automatically.
        """
        articles = []
        url = f"{self.base_url}/api/v2/help_center/{DEFAULT_LOCALE}/articles.json"
        params = {"per_page": PER_PAGE, "sort_by": "updated_at", "sort_order": "desc"}

        page = 1
        while url:
            logger.info(f"Fetching articles page {page}...")
            try:
                response = self.session.get(url, params=params if page == 1 else None, timeout=30)
                response.raise_for_status()
                data = response.json()

                page_articles = data.get("articles", [])
                articles.extend(page_articles)
                logger.info(f"  Got {len(page_articles)} articles (total: {len(articles)})")

                # Pagination
                url = data.get("next_page")
                page += 1
                params = None  # next_page URL already includes params

                # Rate limiting
                time.sleep(RATE_LIMIT_PAUSE)

            except requests.exceptions.RequestException as e:
                logger.error(f"API request failed: {e}")
                break

        logger.info(f"Total articles fetched: {len(articles)}")
        return articles

    def fetch_article_body(self, article: dict) -> str:
        """
        Extract article body HTML. Zendesk API returns it in the article object.
        Falls back to fetching individual article if body is missing.
        """
        body = article.get("body", "")
        if body:
            return body

        # Fallback: fetch individual article
        article_id = article.get("id")
        try:
            url = f"{self.base_url}/api/v2/help_center/{DEFAULT_LOCALE}/articles/{article_id}.json"
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            data = response.json()
            return data.get("article", {}).get("body", "")
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch article {article_id}: {e}")
            return ""
