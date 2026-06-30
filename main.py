import argparse
import logging
import sys
from dotenv import load_dotenv

from scraper import OptiSignsScraper

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def run_scraper() -> dict:
    """Run the scraping pipeline and return stats."""
    logger.info("Starting scraper pipeline...")
    scraper = OptiSignsScraper()
    stats = scraper.run()
    return stats


def run_upload(provider: str = "gemini") -> dict:
    """Run the vector store / file search store upload pipeline and return stats."""
    if provider == "openai":
        from uploader import VectorStoreUploader
        logger.info("Starting OpenAI Vector Store upload pipeline...")
        uploader = VectorStoreUploader()
    else:
        from uploader import GeminiFileSearchUploader
        logger.info("Starting Gemini File Search Store upload pipeline...")
        uploader = GeminiFileSearchUploader()

    stats = uploader.run()
    return stats


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Echo-Vault: support article scraper & vector store uploader"
    )
    parser.add_argument(
        "--scrape", action="store_true",
        help="Only run scraper (no upload)"
    )
    parser.add_argument(
        "--upload", action="store_true",
        help="Only run uploader (no scrape)"
    )
    parser.add_argument(
        "--provider", choices=["gemini", "openai"], default="gemini",
        help="Upload provider: 'gemini' (default) or 'openai'"
    )
    args = parser.parse_args()

    # If no flags specified, run both
    run_both = not args.scrape and not args.upload

    scrape_stats = None
    upload_stats = None

    # ---- Step 1: Scrape ----
    if args.scrape or run_both:
        scrape_stats = run_scraper()

        if scrape_stats["total_fetched"] == 0:
            logger.error("Scraper fetched 0 articles. Aborting.")
            sys.exit(1)

        total_saved = scrape_stats["added"] + scrape_stats["updated"] + scrape_stats["skipped"]
        logger.info(f"Scraper done: {total_saved} articles processed "
                     f"(+{scrape_stats['added']} new, "
                     f"~{scrape_stats['updated']} updated, "
                     f"={scrape_stats['skipped']} skipped)")

    # ---- Step 2: Upload to File Search Store / Vector Store ----
    if args.upload or run_both:
        upload_stats = run_upload(provider=args.provider)
        logger.info(f"Uploader done: {upload_stats.get('uploaded', 0)} files uploaded, "
                     f"{upload_stats.get('skipped', 0)} skipped")

    # ---- Summary ----
    logger.info("")
    logger.info("=" * 60)
    logger.info("PIPELINE COMPLETE")
    logger.info("=" * 60)

    if scrape_stats:
        logger.info(f"  Scrape - Fetched: {scrape_stats['total_fetched']}, "
                     f"Added: {scrape_stats['added']}, "
                     f"Updated: {scrape_stats['updated']}, "
                     f"Skipped: {scrape_stats['skipped']}, "
                     f"Errors: {scrape_stats['errors']}")

    if upload_stats:
        logger.info(f"  Upload - Uploaded: {upload_stats.get('uploaded', 0)}, "
                     f"Skipped: {upload_stats.get('skipped', 0)}, "
                     f"Errors: {upload_stats.get('errors', 0)}")

    logger.info("=" * 60)
    logger.info("Exit 0 - Success")
    sys.exit(0)


if __name__ == "__main__":
    main()
