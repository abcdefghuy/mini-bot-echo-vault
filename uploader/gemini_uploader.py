import os
import json
import time
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from google import genai
from google.genai import types

from utils import compute_hash

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
FILE_SEARCH_STORE_NAME = "support-docs"
UPLOAD_HASHES_FILE = "upload_hashes.json"
EMBEDDING_MODEL = "models/gemini-embedding-2"
MAX_WORKERS_UPLOAD = 10        # Concurrent upload threads
MAX_POLL_RETRIES = 120         # Max retries when polling for batch indexing
POLL_INTERVAL_SECONDS = 2      # Seconds between status polls
BATCH_SIZE = 20                # Upload N files, then brief pause


class GeminiFileSearchUploader:

    def __init__(
        self,
        articles_dir: str = None,
        store_display_name: str = FILE_SEARCH_STORE_NAME,
        max_workers: int = MAX_WORKERS_UPLOAD,
    ):
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY environment variable is required")

        self.client = genai.Client(api_key=api_key)
        self.articles_dir = Path(articles_dir or os.getenv("OUTPUT_DIR", "articles"))
        self.store_display_name = store_display_name
        self.upload_hashes_file = Path(os.getenv("UPLOAD_HASHES_FILE", UPLOAD_HASHES_FILE))
        self.max_workers = max_workers

        # Stats
        self.stats = {
            "uploaded": 0,
            "skipped": 0,
            "errors": 0,
            "total_files": 0,
            "total_documents_in_store": 0,
        }

    # ------------------------------------------------------------------
    # Hash tracking for delta uploads
    # ------------------------------------------------------------------
    def _load_upload_hashes(self) -> dict:
        """Load previously uploaded file hashes."""
        if self.upload_hashes_file.exists():
            with open(self.upload_hashes_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save_upload_hashes(self, hashes: dict):
        """Save uploaded file hashes."""
        with open(self.upload_hashes_file, "w", encoding="utf-8") as f:
            json.dump(hashes, f, indent=2, ensure_ascii=False)

    @staticmethod
    def _compute_file_hash(file_path: Path) -> str:
        """Compute SHA-256 hash of a file."""
        content = file_path.read_text(encoding="utf-8")
        return compute_hash(content)

    # ------------------------------------------------------------------
    # File Search Store management
    # ------------------------------------------------------------------
    def _get_or_create_store(self) -> str:
        """
        Get existing File Search Store by display_name or create a new one.
        Returns the store resource name (e.g. 'fileSearchStores/abc123').
        """
        # List existing stores and find by display name
        try:
            for store in self.client.file_search_stores.list():
                if store.display_name == self.store_display_name:
                    logger.info(f"Found existing File Search Store: {store.name} ({store.display_name})")
                    return store.name
        except Exception as e:
            logger.warning(f"Could not list existing stores: {e}")

        # Create new store
        store = self.client.file_search_stores.create(
            config={
                "display_name": self.store_display_name,
                "embedding_model": EMBEDDING_MODEL,
            }
        )
        logger.info(f"Created new File Search Store: {store.name} ({store.display_name})")
        return store.name

    # ------------------------------------------------------------------
    # Single file upload (called from thread pool)
    # ------------------------------------------------------------------
    def _upload_single_file(
        self, file_path: Path, store_name: str, index: int, total: int
    ) -> dict:
        """
        Upload a single Markdown file to the Gemini File Search Store.
        Fire-and-forget: uploads the file but does NOT poll for indexing completion.
        Returns result dict.

        Designed to be called concurrently from a thread pool.
        """
        max_retries = 3
        backoff = 2
        for attempt in range(1, max_retries + 1):
            try:
                operation = self.client.file_search_stores.upload_to_file_search_store(
                    file=str(file_path),
                    file_search_store_name=store_name,
                    config={"display_name": file_path.stem, "mime_type": "text/markdown"},
                )
                logger.info(f"[{index}/{total}] Uploaded: {file_path.name}")

                # Try to get document name from operation
                doc_name = f"uploaded:{file_path.name}"
                if hasattr(operation, "result") and operation.result:
                    if hasattr(operation.result, "name"):
                        doc_name = operation.result.name

                return {
                    "status": "uploaded",
                    "file_name": file_path.name,
                    "doc_name": doc_name,
                    "operation_name": getattr(operation, "name", None),
                }

            except Exception as e:
                if attempt == max_retries:
                    logger.error(f"[{index}/{total}] Failed after {max_retries} attempts: {file_path.name} — {e}")
                    return {
                        "status": "error",
                        "file_name": file_path.name,
                        "error": str(e),
                    }
                else:
                    logger.warning(f"[{index}/{total}] Attempt {attempt} failed for {file_path.name}: {e}. Retrying in {backoff}s...")
                    time.sleep(backoff)
                    backoff *= 2

    # ------------------------------------------------------------------
    # Batch upload with concurrency
    # ------------------------------------------------------------------
    def _upload_batch_concurrent(
        self, files_to_upload: list[tuple[Path, str]], store_name: str
    ) -> list[dict]:
        """
        Upload a batch of files concurrently using ThreadPoolExecutor.
        Returns list of result dicts.
        """
        results = []
        total = len(files_to_upload)

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {}
            for i, (file_path, file_hash) in enumerate(files_to_upload, 1):
                future = executor.submit(
                    self._upload_single_file, file_path, store_name, i, total
                )
                futures[future] = (file_path, file_hash)

            for future in as_completed(futures):
                file_path, file_hash = futures[future]
                result = future.result()
                result["file_hash"] = file_hash
                result["file_path"] = file_path
                results.append(result)

        return results

    # ------------------------------------------------------------------
    # Cleanup old documents (concurrent)
    # ------------------------------------------------------------------
    def _remove_old_documents(self, store_name: str, old_doc_names: list[str]):
        """Remove documents from the store that are no longer needed."""
        valid_names = [
            name for name in old_doc_names
            if not name.startswith("pending:") and not name.startswith("uploaded:")
        ]
        if not valid_names:
            return

        def _delete_one(doc_name):
            try:
                self.client.file_search_stores.documents.delete(name=doc_name)
                logger.info(f"  Removed: {doc_name}")
            except Exception as e:
                logger.warning(f"  Could not remove {doc_name}: {e}")

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            list(executor.map(_delete_one, valid_names))

    # ------------------------------------------------------------------
    # Count documents in store
    # ------------------------------------------------------------------
    def _is_store_empty(self, store_name: str) -> bool:
        """Check if the File Search Store has no documents (fast check)."""
        try:
            # Generator loop will only request the first page of results from the API.
            for _ in self.client.file_search_stores.documents.list(parent=store_name):
                return False
            return True
        except Exception as e:
            logger.warning(f"Could not check if store is empty: {e}")
            return True

    def _count_documents_in_store(self, store_name: str) -> int:
        """Count the total number of documents in the File Search Store."""
        try:
            docs = list(self.client.file_search_stores.documents.list(parent=store_name))
            return len(docs)
        except Exception as e:
            logger.warning(f"Could not count documents in store: {e}")
            return 0

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    def run(self) -> dict:
        """
        Run the upload pipeline:
        1. Scan articles directory for .md files
        2. Compare hashes to detect changes
        3. Upload new/changed files CONCURRENTLY to Gemini File Search Store
        4. Log file and document counts

        Returns:
            dict with stats: uploaded, skipped, errors, total_files, total_documents_in_store
        """
        start_time = time.time()

        logger.info("=" * 60)
        logger.info("Gemini File Search Store Uploader - Starting")
        logger.info(f"  Concurrency: {self.max_workers} threads")
        logger.info("=" * 60)

        if not self.articles_dir.exists():
            logger.error(f"Articles directory not found: {self.articles_dir}")
            self.stats["errors"] += 1
            return self.stats

        # Get all markdown files
        md_files = sorted(self.articles_dir.glob("*.md"))
        self.stats["total_files"] = len(md_files)
        logger.info(f"Found {len(md_files)} markdown files in {self.articles_dir}")

        if not md_files:
            logger.warning("No markdown files found. Nothing to upload.")
            return self.stats

        # Get or create File Search Store
        store_name = self._get_or_create_store()

        # Fast check if store is empty (e.g. newly created or reset)
        if self._is_store_empty(store_name):
            logger.info("File Search Store is empty. Ignoring upload cache and uploading all files.")
            upload_hashes = {}
        else:
            # Load existing upload hashes
            upload_hashes = self._load_upload_hashes()
        new_hashes = {}
        old_doc_names_to_remove = []
        files_to_upload = []  # list of (file_path, file_hash)

        # Phase 1: Compute hashes & filter changed files (fast, sequential)
        logger.info("Phase 1: Computing hashes & detecting changes...")
        for file_path in md_files:
            file_hash = self._compute_file_hash(file_path)
            file_key = file_path.name

            existing = upload_hashes.get(file_key, {})
            old_hash = existing.get("hash", "")
            old_doc_name = existing.get("doc_name", "")

            if file_hash == old_hash and old_doc_name:
                # File unchanged — skip
                self.stats["skipped"] += 1
                new_hashes[file_key] = existing
                continue

            # File is new or changed
            if old_doc_name:
                old_doc_names_to_remove.append(old_doc_name)

            files_to_upload.append((file_path, file_hash))

        logger.info(f"  → {len(files_to_upload)} files to upload, "
                     f"{self.stats['skipped']} skipped (unchanged)")

        # Phase 2: Concurrent uploads in batches
        if files_to_upload:
            logger.info(f"Phase 2: Uploading {len(files_to_upload)} files "
                         f"({self.max_workers} concurrent threads)...")

            # Process in batches for better progress visibility
            for batch_start in range(0, len(files_to_upload), BATCH_SIZE):
                batch = files_to_upload[batch_start:batch_start + BATCH_SIZE]
                batch_num = (batch_start // BATCH_SIZE) + 1
                total_batches = (len(files_to_upload) + BATCH_SIZE - 1) // BATCH_SIZE

                logger.info(f"  Batch {batch_num}/{total_batches} "
                             f"({len(batch)} files)...")

                results = self._upload_batch_concurrent(batch, store_name)

                for result in results:
                    if result["status"] == "uploaded":
                        self.stats["uploaded"] += 1
                        file_key = result["file_path"].name
                        new_hashes[file_key] = {
                            "hash": result["file_hash"],
                            "doc_name": result["doc_name"],
                        }
                    else:
                        self.stats["errors"] += 1

                # Brief pause between batches to avoid API throttling
                if batch_start + BATCH_SIZE < len(files_to_upload):
                    time.sleep(1)

        # Phase 3: Remove old document versions (concurrent)
        if old_doc_names_to_remove:
            logger.info(f"Phase 3: Removing {len(old_doc_names_to_remove)} old documents...")
            self._remove_old_documents(store_name, old_doc_names_to_remove)

        # Save updated hashes
        self._save_upload_hashes(new_hashes)

        # Count total documents in store
        self.stats["total_documents_in_store"] = self._count_documents_in_store(store_name)

        elapsed = time.time() - start_time

        # Print summary
        logger.info("")
        logger.info("=" * 60)
        logger.info("UPLOAD COMPLETE - Summary")
        logger.info("=" * 60)
        logger.info(f"  Total files found:      {self.stats['total_files']}")
        logger.info(f"  Uploaded (new/updated):  {self.stats['uploaded']}")
        logger.info(f"  Skipped (no change):     {self.stats['skipped']}")
        logger.info(f"  Errors:                  {self.stats['errors']}")
        logger.info(f"  Docs in store:           {self.stats['total_documents_in_store']}")
        logger.info(f"  File Search Store:       {store_name}")
        logger.info(f"  Threads used:            {self.max_workers}")
        logger.info(f"  Elapsed time:            {elapsed:.1f}s")
        logger.info("=" * 60)

        return self.stats

    # ------------------------------------------------------------------
    # Test query helper
    # ------------------------------------------------------------------
    def test_query(self, query: str, store_name: str = None) -> str:
        """
        Run a test query against the File Search Store to verify it works.
        Returns the model response text.
        """
        if not store_name:
            store_name = self._get_or_create_store()

        logger.info(f"Testing query: '{query}' against store: {store_name}")

        response = self.client.models.generate_content(
            model="gemini-2.5-flash",
            contents=query,
            config=types.GenerateContentConfig(
                tools=[
                    types.Tool(
                        file_search=types.FileSearch(
                            file_search_store_names=[store_name]
                        )
                    )
                ]
            ),
        )

        answer = response.text
        logger.info(f"Response: {answer[:200]}...")

        # Log grounding metadata / citations if available
        if response.candidates and response.candidates[0].grounding_metadata:
            metadata = response.candidates[0].grounding_metadata
            logger.info(f"Grounding metadata present: {bool(metadata)}")

        return answer
