"""
uploader/uploader.py - Upload Markdown files to OpenAI Vector Store via API.

This module handles:
1. Creating/reusing an OpenAI Vector Store
2. Uploading Markdown files
3. Attaching files to the Vector Store
4. Connecting the Vector Store to an Assistant
5. Delta detection - only upload new/changed files

Chunking strategy:
- We rely on OpenAI's built-in file_search chunking (auto strategy)
- OpenAI splits documents into ~800 token chunks with 400 token overlap
- This provides good context preservation for support articles
"""

import os
import json
import logging
from pathlib import Path

from openai import OpenAI

from utils import compute_hash

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VECTOR_STORE_NAME = "optisigns-support-docs"
UPLOAD_HASHES_FILE = "upload_hashes.json"


class VectorStoreUploader:
    """
    Uploads Markdown files to OpenAI Vector Store via API.
    Tracks uploaded files via content hashing to support delta uploads.
    """

    def __init__(
        self,
        articles_dir: str = None,
        assistant_id: str = None,
        vector_store_name: str = VECTOR_STORE_NAME,
    ):
        self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.articles_dir = Path(articles_dir or os.getenv("OUTPUT_DIR", "articles"))
        self.assistant_id = assistant_id or os.getenv("OPENAI_ASSISTANT_ID")
        self.vector_store_name = vector_store_name
        self.upload_hashes_file = Path(UPLOAD_HASHES_FILE)

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

        self.client.assistants.update(
            assistant_id=self.assistant_id,
            tool_resources={
                "file_search": {
                    "vector_store_ids": [vector_store_id]
                }
            }
        )
        logger.info(f"Attached Vector Store {vector_store_id} to Assistant {self.assistant_id}")

    # ------------------------------------------------------------------
    # File upload
    # ------------------------------------------------------------------
    def _upload_file_to_vector_store(self, file_path: Path, vector_store_id: str) -> str | None:
        """
        Upload a single file to OpenAI and attach to vector store.
        Returns the file ID or None on error.
        """
        try:
            # Step 1: Upload file to OpenAI Files
            with open(file_path, "rb") as f:
                uploaded_file = self.client.files.create(
                    file=f,
                    purpose="assistants"
                )
            logger.info(f"  Uploaded file: {uploaded_file.id} ({file_path.name})")

            # Step 2: Attach file to vector store
            self.client.vector_stores.files.create(
                vector_store_id=vector_store_id,
                file_id=uploaded_file.id,
            )
            logger.info(f"  Attached to Vector Store: {vector_store_id}")

            return uploaded_file.id

        except Exception as e:
            logger.error(f"  Failed to upload {file_path.name}: {e}")
            return None

    # ------------------------------------------------------------------
    # Cleanup old files
    # ------------------------------------------------------------------
    def _remove_old_files(self, vector_store_id: str, old_file_ids: list[str]):
        """Remove files from vector store that are no longer needed."""
        for file_id in old_file_ids:
            try:
                self.client.vector_stores.files.delete(
                    vector_store_id=vector_store_id,
                    file_id=file_id,
                )
                self.client.files.delete(file_id)
                logger.info(f"  Removed old file: {file_id}")
            except Exception as e:
                logger.warning(f"  Could not remove file {file_id}: {e}")

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    def run(self) -> dict:
        """
        Run the upload pipeline:
        1. Scan articles directory for .md files
        2. Compare hashes to detect changes
        3. Upload only new/changed files
        4. Attach vector store to assistant

        Returns:
            dict with stats: uploaded, skipped, errors, total_files
        """
        logger.info("=" * 60)
        logger.info("Vector Store Uploader - Starting")
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

        # Process each file
        for i, file_path in enumerate(md_files, 1):
            logger.info(f"[{i}/{len(md_files)}] Processing: {file_path.name}")

            file_hash = self._compute_file_hash(file_path)
            file_key = file_path.name

            existing = upload_hashes.get(file_key, {})
            old_hash = existing.get("hash", "")
            old_file_id = existing.get("file_id", "")

            if file_hash == old_hash and old_file_id:
                # File unchanged
                self.stats["skipped"] += 1
                new_hashes[file_key] = existing  # keep existing record
                logger.info(f"  ⏭️  SKIPPED (unchanged)")
                continue

            # File is new or changed - upload it
            if old_file_id:
                # Mark old file for removal
                old_file_ids_to_remove.append(old_file_id)

            file_id = self._upload_file_to_vector_store(file_path, vector_store_id)

            if file_id:
                self.stats["uploaded"] += 1
                new_hashes[file_key] = {
                    "hash": file_hash,
                    "file_id": file_id,
                }
                logger.info(f"  ✅ UPLOADED: {file_path.name}")
            else:
                self.stats["errors"] += 1

        # Remove old versions of updated files
        if old_file_ids_to_remove:
            logger.info(f"Removing {len(old_file_ids_to_remove)} old file versions...")
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
        logger.info("=" * 60)

        return self.stats
