"""Download the SAT practice test PDFs this suite's question bank was sourced from.

The PDFs are College Board copyrighted material and are not redistributed in
this repository. Find "SAT Practice Test 1" (digital) and its answer key on
the official practice test page, and pass their URLs here:

    https://satsuite.collegeboard.org/practice/practice-tests/paper

Usage:
    python data/fetch_sat.py --test-url <url> --answers-url <url>

Downloads land in data/pdfs/ (gitignored). The transcribed question bank
(suites/stereotype_threat/questions.json) is also College Board material, so
it is kept locally and gitignored too; these PDFs are its source.
"""

import argparse
import urllib.request
from pathlib import Path

PDF_DIR = Path(__file__).parent / "pdfs"


def download(url: str, dest: Path) -> None:
    PDF_DIR.mkdir(exist_ok=True)
    urllib.request.urlretrieve(url, dest)
    print(f"Saved {dest}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-url", help="URL of the SAT practice test PDF")
    parser.add_argument("--answers-url", help="URL of the answer key PDF")
    args = parser.parse_args()

    if not args.test_url and not args.answers_url:
        parser.error(
            "no URLs given; find them at "
            "https://satsuite.collegeboard.org/practice/practice-tests/paper "
            "and pass --test-url / --answers-url"
        )

    if args.test_url:
        download(args.test_url, PDF_DIR / "sat-practice-test-1-digital.pdf")
    if args.answers_url:
        download(args.answers_url, PDF_DIR / "sat-practice-test-1-answers-digital.pdf")


if __name__ == "__main__":
    main()
