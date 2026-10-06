"""Bounded child-process execution keeps PyMuPDF off ASGI/event-loop threads."""
import asyncio
import json
import os
import subprocess
import sys

from app.services.legal_pdf_extraction import PdfProblem


def _run(payload: dict, timeout: int) -> dict:
    # Uvicorn --reload uses a Windows SelectorEventLoop, which cannot use
    # asyncio.create_subprocess_exec. Only the process launcher/wait runs in a
    # thread; every PyMuPDF operation still runs in its own child process.
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        process = subprocess.run(
            [sys.executable, "-m", "app.services.legal_pdf_extraction"],
            input=json.dumps(payload).encode(), stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=timeout, **options,
        )
        if process.returncode:
            raise PdfProblem("pdf_process_failed")
        try:
            result = json.loads(process.stdout)
            if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
                raise ValueError("Invalid child response")
        except (ValueError, UnicodeError) as exc:
            raise PdfProblem("pdf_process_invalid_output") from exc
        if not result["ok"]:
            raise PdfProblem(result["error"])
        return result["result"]
    except subprocess.TimeoutExpired as exc:
        raise PdfProblem("page_timeout") from exc
    except OSError as exc:
        raise PdfProblem("pdf_process_unavailable") from exc


async def pdf_process(payload: dict, timeout: int) -> dict:
    return await asyncio.to_thread(_run, payload, timeout)
