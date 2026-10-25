"""End-to-end tests for POST/GET /contracts with a fake repository.

The filesystem write in save_pdf_to_disk is real (pointed at tmp_path by the
`contracts_api` fixture); only the database layer is faked, the same pattern
tests/test_ask.py uses for the law repository.
"""

import fitz

from tests.test_document_upload import _pdf_bytes


def _upload(client, *, pages=1, with_text=True, question=None):
    data = _pdf_bytes(pages=pages, with_text=with_text)
    files = {"file": ("contract.pdf", data, "application/pdf")}
    form = {"question": question} if question is not None else {}
    return client.post("/contracts", files=files, data=form)


def test_upload_saves_and_returns_uploaded_status(contracts_api):
    res = _upload(contracts_api, pages=2)
    assert res.status_code == 201
    body = res.json()

    assert body["status"] == "uploaded"
    assert body["page_count"] == 2
    assert body["version_number"] == 1
    assert body["document_id"].startswith("doc:")
    assert body["version_id"].startswith("docver:")
    assert body["question"] is None
    assert body["error_message"] is None


def test_upload_stores_the_question_for_later(contracts_api):
    res = _upload(contracts_api, question="Is there a termination clause?")
    body = res.json()
    assert body["question"] == "Is there a termination clause?"
    assert "question will be answered" in body["message"]


def test_upload_without_question_still_works_exactly_like_before(contracts_api):
    """No question attached: behaves like a plain file upload."""
    res = _upload(contracts_api)
    body = res.json()
    assert body["question"] is None
    assert res.status_code == 201


def test_file_is_actually_written_to_disk(contracts_api):
    res = _upload(contracts_api, pages=1)
    repository = contracts_api.state["repository"]
    call = repository.created_calls[0]
    with open(call["storage_path"], "rb") as f:
        saved = fitz.open(stream=f.read(), filetype="pdf")
    assert saved.page_count == 1


def test_non_pdf_upload_is_rejected(contracts_api):
    files = {"file": ("notes.txt", b"hello world", "text/plain")}
    res = contracts_api.post("/contracts", files=files)
    assert res.status_code == 422
    assert "PDF" in res.json()["detail"]


def test_scanned_pdf_with_no_text_is_rejected(contracts_api):
    res = _upload(contracts_api, with_text=False)
    assert res.status_code == 422
    assert "scanned" in res.json()["detail"]


def test_too_many_pages_is_rejected(contracts_api):
    res = _upload(contracts_api, pages=11)
    assert res.status_code == 422


def test_get_unknown_document_is_404(contracts_api):
    res = contracts_api.get("/contracts/doc:does-not-exist")
    assert res.status_code == 404


def test_get_after_upload_returns_the_same_record(contracts_api):
    uploaded = _upload(contracts_api).json()
    res = contracts_api.get(f"/contracts/{uploaded['document_id']}")
    assert res.status_code == 200
    body = res.json()
    assert body["document_id"] == uploaded["document_id"]
    assert body["version_id"] == uploaded["version_id"]
    assert body["status"] == "uploaded"


def test_openapi_lists_contracts_routes(contracts_api):
    paths = contracts_api.get("/openapi.json").json()["paths"]
    assert "/contracts" in paths
    assert "/contracts/{document_id}" in paths


def test_phase_c_ask_route_is_separate_from_contracts_router(contracts_api):
    """Phase C owns /ask; contracts still handles only uploads and retrieval."""
    assert "/ask" in contracts_api.get("/openapi.json").json()["paths"]
    res = contracts_api.post("/ask", json={"question": "hi"})
    assert res.status_code == 404  # Search is disabled by the isolated test fixture.
