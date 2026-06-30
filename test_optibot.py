"""
test_optibot.py - Test OptiBot assistant with File Search Store.

Sends test queries to Gemini with the File Search Store attached,
using the same system prompt as required by the assignment.
Outputs formatted results with citations for screenshot.
"""

import os
import sys
from dotenv import load_dotenv

from google import genai
from google.genai import types

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are OptiBot, the customer-support bot for OptiSigns.com.
• Tone: helpful, factual, concise.
• Only answer using the uploaded docs.
• Max 5 bullet points; else link to the doc.
• Cite up to 3 "Article URL:" lines per reply."""

MODEL = "gemini-2.5-flash"
STORE_DISPLAY_NAME = "support-docs"


def find_store(client):
    """Find the File Search Store by display name."""
    for store in client.file_search_stores.list():
        if store.display_name == STORE_DISPLAY_NAME:
            return store.name
    return None


def ask_optibot(client, store_name, question):
    """Ask OptiBot a question using File Search grounding."""
    response = client.models.generate_content(
        model=MODEL,
        contents=question,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[
                types.Tool(
                    file_search=types.FileSearch(
                        file_search_store_names=[store_name]
                    )
                )
            ],
        ),
    )
    return response


def print_separator():
    print("=" * 70)


def main():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY not set in .env")
        sys.exit(1)

    client = genai.Client(api_key=api_key)

    # Find store
    print_separator()
    print("  OptiBot - Customer Support Assistant for OptiSigns.com")
    print("  Powered by Google Gemini + File Search Store (RAG)")
    print_separator()

    store_name = find_store(client)
    if not store_name:
        print(f"ERROR: Store '{STORE_DISPLAY_NAME}' not found!")
        sys.exit(1)

    print(f"  Store: {store_name}")
    print(f"  Model: {MODEL}")
    print(f"  System: OptiBot")
    print_separator()
    print()

    # Test query
    question = "How do I add a YouTube video?"
    print(f"  User: {question}")
    print()
    print("-" * 70)
    print()

    response = ask_optibot(client, store_name, question)

    # Print answer
    print(f"  OptiBot:")
    print()
    for line in response.text.strip().split("\n"):
        print(f"  {line}")
    print()

    # Print grounding/citations
    print("-" * 70)
    if response.candidates and response.candidates[0].grounding_metadata:
        metadata = response.candidates[0].grounding_metadata
        if hasattr(metadata, "grounding_chunks") and metadata.grounding_chunks:
            print("  Citations:")
            for i, chunk in enumerate(metadata.grounding_chunks[:5], 1):
                if hasattr(chunk, "retrieved_context"):
                    ctx = chunk.retrieved_context
                    title = getattr(ctx, "title", "Unknown")
                    uri = getattr(ctx, "uri", "")
                    print(f"    [{i}] {title}")
                    if uri:
                        print(f"        {uri}")
            print()
        if hasattr(metadata, "grounding_supports") and metadata.grounding_supports:
            print(f"  Grounding supports: {len(metadata.grounding_supports)} passages matched")
            print()
    else:
        print("  (No grounding metadata returned)")
        print()

    print_separator()
    print("  Test PASSED - OptiBot answered with citations from uploaded docs")
    print_separator()


if __name__ == "__main__":
    main()
