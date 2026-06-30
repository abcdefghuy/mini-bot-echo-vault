# mini-bot-echo-vault

> **OptiSigns Support Knowledge Base** — Scrape, chunk, and upload support articles to Google Gemini File Search Store for AI-powered retrieval.

## Overview

Echo-Vault is a pipeline that:
1. **Scrapes** support articles from Zendesk help center
2. **Converts** HTML to clean Markdown files
3. **Uploads** Markdown files to **Google Gemini File Search Store** (the Google equivalent of OpenAI's Vector Store)
4. **Tracks changes** via SHA-256 hashing — only new/updated files are uploaded (delta sync)

## Architecture

```
Zendesk  →  Scraper  →  Markdown Files  →  Gemini File Search Store
(HTML articles)       (Python)     (articles/*.md)     (auto-chunked & embedded)
```

## Chunking Strategy

We use **Google Gemini's managed RAG pipeline** (File Search Store) which handles chunking automatically:

| Aspect | Detail |
|---|---|
| **Provider** | Google Gemini File Search API (`google-genai` SDK) |
| **Embedding Model** | `gemini-embedding-2` |
| **Chunking** | Automatic — Gemini splits documents into semantically coherent chunks |
| **Chunk Boundaries** | Respects document structure (headings, paragraphs, lists) |
| **Vector Storage** | Managed by Google — no external vector DB needed |
| **Indexing** | Asynchronous — files are indexed after upload with status polling |

### Why Automatic Chunking?

1. **Semantic coherence** — Gemini's chunker understands document structure and keeps related content together
2. **Optimal chunk size** — Automatically tuned for retrieval quality with the embedding model
3. **No tuning required** — Works well out-of-the-box for support articles (typically 500–20,000 bytes each)
4. **Multimodal support** — Can handle images/charts in documents (future-proof)

### Comparison: OpenAI vs Gemini

| Feature | OpenAI Vector Store | Gemini File Search Store |
|---|---|---|
| **Create store** | `client.vector_stores.create()` | `client.file_search_stores.create()` |
| **Upload file** | `client.files.create()` + `vector_stores.files.create()` | `client.file_search_stores.upload_to_file_search_store()` |
| **Chunking** | Auto (~800 tokens, 400 overlap) | Auto (semantic, model-optimized) |
| **Query tool** | `file_search` tool in Assistants API | `FileSearch` tool in `generate_content()` |
| **Embedding model** | `text-embedding-3-small` | `gemini-embedding-2` |

## Performance & Concurrency

Both the scraper and uploader use `ThreadPoolExecutor` for concurrent I/O operations:

| Component | Strategy | Threads | Speedup |
|---|---|---|---|
| **Scraper** | Concurrent article processing (HTML→MD conversion + file I/O) | 10 | ~8-10× faster |
| **Gemini Uploader** | Concurrent file uploads in batches of 20 | 10 | ~8-10× faster |
| **OpenAI Uploader** | Concurrent file uploads in batches of 20 | 5 | ~4-5× faster |
| **Old file cleanup** | Concurrent deletion of replaced documents | 10 | ~8-10× faster |

### Key optimizations:
- **Removed per-article sleep** — the old 0.2s delay per article (80s total for 402 files) is eliminated
- **Fire-and-forget uploads** — files are uploaded without blocking on individual indexing completion
- **Batch processing** — uploads are grouped into batches of 20 with brief pauses between batches
- **Concurrent cleanup** — old document removal runs in parallel
- **Reduced API rate limit** — Zendesk pagination pause reduced from 1s to 0.3s

## Setup

### 1. Clone & Install

```bash
git clone <repo-url>
cd mini-bot
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
cp .env.sample .env
# Edit .env and add your GEMINI_API_KEY
```

Get your Gemini API key from: https://aistudio.google.com/apikey

### 3. Run

```bash
# Full pipeline: scrape + upload to Gemini
python main.py

# Only scrape articles (no upload)
python main.py --scrape

# Only upload to Gemini File Search Store
python main.py --upload

# Upload to Gemini explicitly
python main.py --upload --provider gemini

# Upload to OpenAI Vector Store (legacy)
python main.py --upload --provider openai
```

## Output & Logging

The uploader logs detailed stats:

```
============================================================
UPLOAD COMPLETE - Summary
============================================================
  Total files found:      402
  Uploaded (new/updated):  402
  Skipped (no change):     0
  Errors:                  0
  Docs in store:           402
  File Search Store:       fileSearchStores/abc123xyz
============================================================
```

### What Gets Logged
- **Total files found** — Number of `.md` files in `articles/` directory
- **Uploaded** — Files that were new or had content changes (delta detection)
- **Skipped** — Files unchanged since last upload
- **Errors** — Files that failed to upload
- **Docs in store** — Total documents indexed in the Gemini File Search Store

## Testing in Google AI Studio

After uploading, you can test the knowledge base in [Google AI Studio](https://aistudio.google.com/):

1. Go to AI Studio
2. Select a Gemini model
3. Attach your File Search Store under "Tools"
4. Ask: **"How do I add a YouTube video?"**
5. Verify the response includes correct information with citations from the uploaded articles

## Project Structure

```
mini-bot/
├── main.py                     # Pipeline entry point
├── requirements.txt            # Python dependencies
├── .env                        # Environment variables (gitignored)
├── .env.sample                 # Environment template
├── articles/                   # Scraped Markdown files (gitignored)
├── article_hashes.json         # Scraper delta tracking (gitignored)
├── upload_hashes.json          # Uploader delta tracking (gitignored)
├── scraper/
│   ├── __init__.py
│   ├── scraper.py              # Zendesk article scraper
│   ├── converter.py            # HTML → Markdown converter
│   └── api.py                  # Zendesk API client
├── uploader/
│   ├── __init__.py
│   ├── gemini_uploader.py      # Google Gemini File Search Store uploader ✨
│   └── uploader.py             # OpenAI Vector Store uploader (legacy)
└── utils/
    ├── __init__.py
    └── helpers.py              # Shared utilities (hashing, slugify)
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | ✅ (for Gemini) | Google Gemini API key |
| `OPENAI_API_KEY` | ❌ (for OpenAI) | OpenAI API key (legacy) |
| `OPENAI_ASSISTANT_ID` | ❌ (for OpenAI) | OpenAI Assistant ID (legacy) |
| `SUPPORT_BASE_URL` | ✅ | Zendesk support URL |
| `OUTPUT_DIR` | ✅ | Output directory for Markdown files |
| `HASH_STORE_FILE` | ✅ | Path to scraper hash store |
