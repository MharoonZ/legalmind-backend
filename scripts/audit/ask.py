"""Print one real /ask response in a human-readable form for source review.

    python -m scripts.audit.ask "What does section 9 of The Prisons Act, 1894 say?"

Sends one request to a running server, performs no retry, and never prints
server secrets.
"""

import argparse

import httpx


def main():
    parser = argparse.ArgumentParser(description="Review one answer and its citations")
    parser.add_argument("question", help="Question to ask the local LegalMind server")
    parser.add_argument("--url", default="http://127.0.0.1:8000/ask")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--api-key", help="X-API-Key, when the server requires one")
    args = parser.parse_args()
    try:
        headers = {"X-API-Key": args.api_key} if args.api_key else {}
        response = httpx.post(args.url, json={"question": args.question}, timeout=args.timeout, headers=headers)
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"Request failed: {type(exc).__name__}. Check the backend terminal and URL.")
        return 1
    print(f"HTTP: {response.status_code}")
    print(f"Status: {data.get('status', 'unknown')}")
    print(f"Publication: {data.get('publication_id') or 'none'}")
    print("Answer:")
    print(data.get("answer", "No answer text returned."))
    if data.get("clarification_question"):
        print("Follow-up:", data["clarification_question"])
    for index, citation in enumerate(data.get("citations", []), 1):
        print(f"\nEvidence {index}: {citation.get('law')} · {citation.get('kind')} "
              f"{citation.get('label')} · PDF page {citation.get('page_index')}")
        print("Evidence ID:", citation.get("evidence_id"))
        print("PDF:", citation.get("filename"))
        print("Quotation:", citation.get("quote"))
        print("Source URL:", citation.get("source_url") or "not recorded")
    if not data.get("citations"):
        print("No citations returned. Do not treat this as a verified legal answer.")
    for note in data.get("limitations", []):
        print("Limitation:", note)
    return 0 if response.status_code < 500 else 1


if __name__ == "__main__":
    raise SystemExit(main())
