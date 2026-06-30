import os
import json
import time
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from openai import OpenAI

from utils import compute_hash

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VECTOR_STORE_NAME = "support-docs"
UPLOAD_HASHES_FILE = "upload_hashes.json"
MAX_WORKERS_UPLOAD = 5         # Concurrent upload threads (OpenAI has stricter rate limits)
BATCH_SIZE = 20                # Upload N files, then brief pause


class VectorStoreUploader:
    """
    Uploads Markdown files to OpenAI Vector Store via API.
    Tracks uploaded files via content hashing to support delta uploads.

    Uses ThreadPoolExecutor for concurrent uploads.
    """

    def __init__(
        self,
        articles_dir: str = None,
        assistant_id: str = None,
        vector_store_name: str = VECTOR_STORE_NAME,
        max_workers: int = MAX_WORKERS_UPLOAD,
    ):
        self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.articles_dir = Path(articles_dir or os.getenv("OUTPUT_DIR", "articles"))
        self.assistant_id = assistant_id or os.getenv("OPENAI_ASSISTANT_ID")
        self.vector_store_name = vector_store_name
        self.upload_hashes_file = Path(os.getenv("UPLOAD_HASHES_FILE", UPLOAD_HASHES_FILE))
        self.max_workers = max_workers

        # Stats
        self.stats = {"uploaded": 0, "skipped": 0, "errors": 0, "total_files": 0, "total_chunks": 0}

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
    # Vector Store management
    # ------------------------------------------------------------------
    def _get_or_create_vector_store(self) -> str:
        """Get existing vector store or create a new one. Returns store ID."""
        # List existing vector stores
        vector_stores = self.client.vector_stores.list()
        for vs in vector_stores.data:
            if vs.name == self.vector_store_name:
                logger.info(f"Found existing Vector Store: {vs.id} ({vs.name})")
                return vs.id

        # Create new vector store
        vector_store = self.client.vector_stores.create(
            name=self.vector_store_name,
            chunking_strategy={
                "type": "auto"
            }
        )
        logger.info(f"Created new Vector Store: {vector_store.id} ({vector_store.name})")
        return vector_store.id

    def _attach_vector_store_to_assistant(self, vector_store_id: str):
        """Attach vector store to the assistant for file_search."""
        if not self.assistant_id:
            logger.warning("No OPENAI_ASSISTANT_ID set. Skipping assistant attachment.")
            return

        self.client.beta.assistants.update(
            assistant_id=self.assistant_id,
            tool_resources={
                "file_search": {
                    "vector_store_ids": [vector_store_id]
                }
            }
        )
        logger.info(f"Attached Vector Store {vector_store_id} to Assistant {self.assistant_id}")

    # ------------------------------------------------------------------
    # Single file upload (called from thread pool)
    # ------------------------------------------------------------------
    def _upload_single_file(
        self, file_path: Path, vector_store_id: str, index: int, total: int
    ) -> dict:
        """
        Upload a single file to OpenAI and attach to vector store.
        Returns result dict.
        """
        try:
            # Step 1: Upload file to OpenAI Files
            with open(file_path, "rb") as f:
                uploaded_file = self.client.files.create(
                    file=f,
                    purpose="assistants"
                )

            # Step 2: Attach file to vector store
            self.client.vector_stores.files.create(
                vector_store_id=vector_store_id,
                file_id=uploaded_file.id,
            )
            logger.info(f"[{index}/{total}] ✅ Uploaded: {file_path.name} ({uploaded_file.id})")

            return {
                "status": "uploaded",
                "file_name": file_path.name,
                "file_id": uploaded_file.id,
            }

        except Exception as e:
            logger.error(f"[{index}/{total}] ❌ Failed: {file_path.name} — {e}")
            return {
                "status": "error",
                "file_name": file_path.name,
                "error": str(e),
            }

    # ------------------------------------------------------------------
    # Cleanup old files (concurrent)
    # ------------------------------------------------------------------
    def _remove_old_files(self, vector_store_id: str, old_file_ids: list[str]):
        """Remove files from vector store that are no longer needed."""
        def _delete_one(file_id):
            try:
                self.client.vector_stores.files.delete(
                    vector_store_id=vector_store_id,
                    file_id=file_id,
                )
                self.client.files.delete(file_id)
                logger.info(f"  Removed: {file_id}")
            except Exception as e:
                logger.warning(f"  Could not remove {file_id}: {e}")

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            list(executor.map(_delete_one, old_file_ids))

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    def run(self) -> dict:
        """
        Run the upload pipeline:
        1. Scan articles directory for .md files
        2. Compare hashes to detect changes
        3. Upload only new/changed files CONCURRENTLY
        4. Attach vector store to assistant

        Returns:
            dict with stats: uploaded, skipped, errors, total_files
        """
        start_time = time.time()

        logger.info("=" * 60)
        logger.info("Vector Store Uploader - Starting")
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

        # Get or create vector store
        vector_store_id = self._get_or_create_vector_store()

        # Load existing upload hashes
        upload_hashes = self._load_upload_hashes()
        new_hashes = {}
        old_file_ids_to_remove = []
        files_to_upload = []  # list of (file_path, file_hash)

        # Phase 1: Compute hashes & filter changed files
        logger.info("Phase 1: Computing hashes & detecting changes...")
        for file_path in md_files:
            file_hash = self._compute_file_hash(file_path)
            file_key = file_path.name

            existing = upload_hashes.get(file_key, {})
            old_hash = existing.get("hash", "")
            old_file_id = existing.get("file_id", "")

            if file_hash == old_hash and old_file_id:
                self.stats["skipped"] += 1
                new_hashes[file_key] = existing
                continue

            if old_file_id:
                old_file_ids_to_remove.append(old_file_id)

            files_to_upload.append((file_path, file_hash))

        logger.info(f"  → {len(files_to_upload)} files to upload, "
                     f"{self.stats['skipped']} skipped (unchanged)")

        # Phase 2: Concurrent uploads in batches
        if files_to_upload:
            logger.info(f"Phase 2: Uploading {len(files_to_upload)} files "
                         f"({self.max_workers} concurrent threads)...")

            for batch_start in range(0, len(files_to_upload), BATCH_SIZE):
                batch = files_to_upload[batch_start:batch_start + BATCH_SIZE]
                batch_num = (batch_start // BATCH_SIZE) + 1
                total_batches = (len(files_to_upload) + BATCH_SIZE - 1) // BATCH_SIZE

                logger.info(f"  Batch {batch_num}/{total_batches} ({len(batch)} files)...")

                with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                    futures = {}
                    for i, (file_path, file_hash) in enumerate(batch, batch_start + 1):
                        future = executor.submit(
                            self._upload_single_file,
                            file_path, vector_store_id, i, len(files_to_upload)
                        )
                        futures[future] = (file_path, file_hash)

                    for future in as_completed(futures):
                        file_path, file_hash = futures[future]
                        result = future.result()

                        if result["status"] == "uploaded":
                            self.stats["uploaded"] += 1
                            new_hashes[file_path.name] = {
                                "hash": file_hash,
                                "file_id": result["file_id"],
                            }
                        else:
                            self.stats["errors"] += 1

                # Brief pause between batches
                if batch_start + BATCH_SIZE < len(files_to_upload):
                    time.sleep(1)

        # Phase 3: Remove old file versions (concurrent)
        if old_file_ids_to_remove:
            logger.info(f"Phase 3: Removing {len(old_file_ids_to_remove)} old files...")
            self._remove_old_files(vector_store_id, old_file_ids_to_remove)

        # Save updated hashes
        self._save_upload_hashes(new_hashes)

        # Attach vector store to assistant
        self._attach_vector_store_to_assistant(vector_store_id)

        # Get vector store stats
        try:
            vs_info = self.client.vector_stores.retrieve(vector_store_id)
            self.stats["total_chunks"] = vs_info.file_counts.completed if hasattr(vs_info, 'file_counts') else 0
            logger.info(f"Vector Store file count: {self.stats['total_chunks']}")
        except Exception:
            pass

        elapsed = time.time() - start_time

        # Print summary
        logger.info("")
        logger.info("=" * 60)
        logger.info("UPLOAD COMPLETE - Summary")
        logger.info("=" * 60)
        logger.info(f"  Total files found:  {self.stats['total_files']}")
        logger.info(f"  Uploaded (new/upd): {self.stats['uploaded']}")
        logger.info(f"  Skipped (no change):{self.stats['skipped']}")
        logger.info(f"  Errors:             {self.stats['errors']}")
        logger.info(f"  Vector Store ID:    {vector_store_id}")
        logger.info(f"  Threads used:       {self.max_workers}")
        logger.info(f"  Elapsed time:       {elapsed:.1f}s")
        logger.info("=" * 60)

        return self.stats
