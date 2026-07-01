"""Generate a GitHub Actions Job Summary from the pipeline log."""

import re
import os


def main():
    if not os.path.exists("pipeline.log"):
        print("Log file not found")
        return

    with open("pipeline.log", "r", encoding="utf-8") as f:
        log_content = f.read()

    # Find summaries
    scrape_match = re.search(
        r"Scrape - Fetched: (\d+), Added: (\d+), Updated: (\d+), Skipped: (\d+), Errors: (\d+)",
        log_content,
    )
    upload_match = re.search(
        r"Upload - Uploaded: (\d+), Skipped: (\d+), Errors: (\d+)",
        log_content,
    )

    summary_md = "## Pipeline Run Summary\n\n"

    if scrape_match:
        f_count, a_count, u_count, s_count, e_count = scrape_match.groups()
        summary_md += "### Zendesk Scraper\n"
        summary_md += "| Metric | Count |\n|---|---|\n"
        summary_md += f"| Total Fetched | {f_count} |\n"
        summary_md += f"| Added (New) | **{a_count}** |\n"
        summary_md += f"| Updated | **{u_count}** |\n"
        summary_md += f"| Skipped | {s_count} |\n"
        summary_md += f"| Errors | {e_count} |\n\n"

    if upload_match:
        up_count, sk_count, err_count = upload_match.groups()
        summary_md += "### Gemini File Search Uploader\n"
        summary_md += "| Metric | Count |\n|---|---|\n"
        summary_md += f"| Uploaded (New/Updated) | **{up_count}** |\n"
        summary_md += f"| Skipped (Unchanged) | {sk_count} |\n"
        summary_md += f"| Errors | {err_count} |\n\n"

    if not scrape_match and not upload_match:
        summary_md += "> No summary data found in pipeline log.\n\n"

    # Write to GitHub Step Summary
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as sf:
            sf.write(summary_md)
        print("Summary written to GITHUB_STEP_SUMMARY")
    else:
        print(summary_md)


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        print(f"Failed to generate summary: {ex}")
