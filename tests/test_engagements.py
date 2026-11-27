"""Focused end-to-end checks for hiring, engagements, cases and multi-party agreements."""

from uuid import uuid4

from sqlalchemy import select

from app.platform.models import AgreementParty, MailOutbox, OtpChallenge
from tests.test_platform import admin_token, post, register, request
from tests.test_platform import platform as platform_fixture
from tests.test_workflow_enhancements import email_settings as email_settings_fixture

platform = platform_fixture
email_settings = email_settings_fixture


def get(client, path, token, status=200):
    result = request(client, "GET", path, token)
    assert result.status_code == status, result.text
    return result.json()


def people(client):
    user, ut = register(client)
    wakeel, wt = register(client, "wakeel", "w")
    post(
        client,
        "/admin/actions",
        admin_token(client),
        {"action": "verify", "resource_id": wakeel["id"], "reason": "Bar registration checked"},
    )
    return user, ut, wakeel, wt


def hire(client, ut, wt, wakeel):
    body = {
        "wakeel_id": wakeel["id"],
        "category": "Property dispute",
        "requirements": "Representation in a boundary dispute",
        "description": "My neighbour built over the boundary line.",
        "communication": "chat",
        "budget": "50000.00",
    }
    hiring = post(client, "/hiring-requests", ut, body, 201)
    post(client, "/hiring-requests", ut, body, 409)  # no duplicate pending request
    accepted = post(client, f"/hiring-requests/{hiring['id']}/actions", wt, {"action": "accept"})
    return accepted["relationship_id"]


CASE = {
    "title": "Boundary wall dispute",
    "category": "Property dispute",
    "description": "Encroachment on the eastern boundary.",
    "proposed_fee": "40000.00",
    "fee_type": "FIXED",
    "details": {"property_address": "Plot 12, Lahore"},
}


def test_hiring_case_lifecycle_termination_and_rehire(platform):
    client = platform
    user, ut, wakeel, wt = people(client)
    outsider, ot = register(client, suffix="outsider")

    # Only the Wakeel responds; the client cannot accept their own request.
    pending = post(
        client,
        "/hiring-requests",
        ut,
        {
            "wakeel_id": wakeel["id"],
            "category": "Family dispute",
            "requirements": "Advice on guardianship",
            "description": "I need advice about guardianship.",
        },
        201,
    )
    post(client, f"/hiring-requests/{pending['id']}/actions", ut, {"action": "accept"}, 403)
    get(client, f"/hiring-requests/{pending['id']}", ot, 404)
    post(client, f"/hiring-requests/{pending['id']}/actions", wt, {"action": "reject", "reason": ""}, 422)
    post(
        client,
        f"/hiring-requests/{pending['id']}/actions",
        wt,
        {"action": "reject", "reason": "Outside my practice area"},
    )

    workspace = hire(client, ut, wt, wakeel)
    get(client, f"/relationships/{workspace}", ot, 404)
    assert get(client, "/dashboard", ut)["counts"]["relationships"] == 1

    # Client proposes; the Wakeel counters the fee; the client accepts fee, the Wakeel accepts the case.
    case = post(client, "/cases", ut, {**CASE, "relationship_id": workspace}, 201)
    assert case["status"] == "SUBMITTED" and case["proposed_by"] == user["id"]
    get(client, f"/cases/{case['id']}", ot, 404)
    post(client, f"/cases/{case['id']}/actions", ut, {"action": "accept"}, 409)  # proposer cannot self-approve
    fee = post(client, f"/cases/{case['id']}/fees", wt, {"amount": "45000.00", "fee_type": "FIXED"}, 201)
    post(client, f"/cases/{case['id']}/fees/{fee['id']}/decision", wt, {"action": "accept"}, 403)
    post(client, f"/cases/{case['id']}/actions", wt, {"action": "accept"}, 409)  # own fee still open
    post(client, f"/cases/{case['id']}/fees/{fee['id']}/decision", ut, {"action": "accept"})
    accepted = post(client, f"/cases/{case['id']}/actions", wt, {"action": "accept"})
    assert accepted["status"] == "ACCEPTED" and float(accepted["agreed_fee"]) == 45000

    # A second, independent case in the same engagement.
    second = post(client, "/cases", wt, {**CASE, "title": "Rent recovery", "relationship_id": workspace}, 201)
    post(client, f"/cases/{second['id']}/actions", ut, {"action": "request_changes", "reason": "Fee is too high"})
    revised = post(
        client, f"/cases/{second['id']}/revise", wt, {**CASE, "title": "Rent recovery", "proposed_fee": "20000.00"}
    )
    assert revised["status"] == "SUBMITTED"
    post(client, f"/cases/{second['id']}/actions", ut, {"action": "accept"})

    # Independent case threads never leak into general chat.
    post(
        client,
        f"/cases/{case['id']}/messages",
        ut,
        {"body": "Boundary survey attached", "request_id": str(uuid4())},
        201,
    )
    assert len(get(client, f"/cases/{case['id']}/messages", wt)["messages"]) == 1
    assert get(client, f"/cases/{second['id']}/messages", wt)["messages"] == []
    get(client, f"/cases/{case['id']}/messages", ot, 404)
    thread = get(client, "/conversations", ut)[0]
    assert get(client, f"/conversations/{thread['id']}", ut)["messages"] == []

    # Work, payments (confirmed only by the Wakeel), completion and rating.
    post(client, f"/cases/{case['id']}/updates", ut, {"note": "Trying to activate", "status": "ACTIVE"}, 403)
    post(client, f"/cases/{case['id']}/updates", wt, {"note": "Work started", "status": "ACTIVE"})
    payment = post(client, f"/cases/{case['id']}/payments", ut, {"amount": "45000.00", "method": "Bank transfer"}, 201)
    assert payment["status"] == "PENDING"
    assert get(client, f"/cases/{case['id']}", ut)["payment_status"] == "UNPAID"
    post(client, f"/cases/{case['id']}/payments/{payment['id']}/decision", ut, {"action": "confirm"}, 403)
    post(client, f"/cases/{case['id']}/payments/{payment['id']}/decision", wt, {"action": "confirm"})
    assert get(client, f"/cases/{case['id']}", ut)["payment_status"] == "PAID"
    post(
        client,
        f"/cases/{case['id']}/deadlines",
        wt,
        {"title": "First hearing", "kind": "hearing", "due_on": "2099-01-10"},
        201,
    )
    assert get(client, "/calendar", ut)[0]["title"] == "First hearing"
    post(client, f"/cases/{case['id']}/rating", ut, rating(), 409)  # not before completion
    post(
        client,
        f"/cases/{case['id']}/actions",
        wt,
        {"action": "request_completion", "reason": "Decree obtained in favour of the client"},
    )
    post(client, f"/cases/{case['id']}/actions", wt, {"action": "confirm_completion"}, 409)
    done = post(client, f"/cases/{case['id']}/actions", ut, {"action": "confirm_completion"})
    assert done["status"] == "COMPLETED"
    post(client, f"/cases/{case['id']}/rating", wt, rating(), 403)
    post(client, f"/cases/{case['id']}/rating", ut, rating(), 201)
    post(client, f"/cases/{case['id']}/rating", ut, rating(), 409)
    detail = get(client, f"/cases/{case['id']}", wt)
    assert detail["rating"]["overall"] == 5 and len(detail["fees"]) == 2
    assert [u["status"] for u in detail["updates"] if u["kind"] == "status"][-1] == "COMPLETED"
    assert get(client, f"/wakeels/{wakeel['id']}", None)["review_count"] == 1

    # Closing one case leaves the engagement active. Ending it is blocked by the open second case.
    assert get(client, f"/relationships/{workspace}", ut)["status"] == "ACTIVE"
    post(client, f"/relationships/{workspace}/rating", ut, relationship_rating(), 409)
    post(
        client,
        f"/relationships/{workspace}/actions",
        ut,
        {"action": "request_termination", "reason": "Matter concluded, thank you"},
    )
    post(
        client, f"/relationships/{workspace}/actions", ut, {"action": "confirm_termination"}, 409
    )  # requester must wait
    blocked = request(client, "POST", f"/relationships/{workspace}/actions", wt, json={"action": "confirm_termination"})
    assert blocked.status_code == 409 and "still open" in blocked.json()["detail"]
    post(
        client,
        f"/cases/{second['id']}/actions",
        wt,
        {"action": "close", "reason": "Client will pursue this separately"},
    )
    ended = post(client, f"/relationships/{workspace}/actions", wt, {"action": "confirm_termination"})
    assert ended["status"] == "ENDED"
    post(client, f"/relationships/{workspace}/rating", ut, relationship_rating(), 201)
    post(client, f"/relationships/{workspace}/rating", ut, relationship_rating(), 409)
    post(client, "/cases", ut, {**CASE, "relationship_id": workspace}, 409)  # ended engagement is read-only

    # History is preserved and a new engagement gets its own record.
    assert get(client, f"/cases/{case['id']}", ut)["status"] == "COMPLETED"
    again = hire(client, ut, wt, wakeel)
    assert again != workspace
    assert {r["status"] for r in get(client, "/relationships", ut)} == {"ACTIVE", "ENDED"}


def rating():
    return {
        "overall": 5,
        "professionalism": 5,
        "communication": 4,
        "responsiveness": 4,
        "quality": 5,
        "body": "Excellent",
    }


def relationship_rating():
    return {
        "overall": 4,
        "professionalism": 5,
        "legal_knowledge": 5,
        "communication": 4,
        "timeliness": 4,
        "fee_transparency": 4,
        "satisfaction": 4,
    }


TERMS = {
    "title": "Shop partnership agreement",
    "agreement_type": "Partnership agreement",
    "description": "Partnership to run a retail shop in Lahore.",
    "scope": "Joint operation of the shop",
    "obligations": "Each partner contributes capital equally",
    "effective_date": "2020-01-01",
    "expiry_date": "2099-12-31",
    "termination": "Either party may exit with 60 days notice",
    "dispute_resolution": "Arbitration in Lahore",
    "governing_law": "Laws of Pakistan; courts at Lahore",
}


def test_multi_party_agreement_invitations_revision_and_signatures(platform):
    client = platform
    user, ut, wakeel, wt = people(client)
    partner, pt = register(client, suffix="partner")
    parties = [
        {"name": user["name"], "email": user["email"], "role": "First partner"},
        {"name": partner["name"], "email": partner["email"], "role": "Second partner"},
        {"name": "New Person", "email": "new@example.test", "role": "Third partner"},
    ]
    post(client, "/agreements/multi-party", wt, {"terms": TERMS, "parties": parties[:1]}, 422)
    # The Wakeel drafts for the parties without being a signatory.
    item = post(client, "/agreements/multi-party", wt, {"terms": TERMS, "parties": parties}, 201)
    path = f"/agreements/{item['id']}"
    assert get(client, path, wt)["drafting_only"] is True
    get(client, path, ut, 404)  # nothing is visible before the draft is sent

    def act(token, name, status=200, **extra):
        revision = get(client, path, wt)["revision"]
        return post(client, path + "/actions", token, {"action": name, "revision": revision, **extra}, status)

    act(wt, "send")
    # Registered parties accept in the app; contents stay hidden until they do.
    get(client, path, pt, 404)
    invitations = get(client, "/agreement-invitations", pt)
    assert len(invitations) == 1 and "title" not in invitations[0]
    post(client, f"/agreement-invitations/{invitations[0]['id']}/accept", pt, {})
    post(client, f"/agreement-invitations/{get(client, '/agreement-invitations', ut)[0]['id']}/accept", ut, {})
    assert len(get(client, path, pt)["parties"]) == 3

    # The unregistered party must register with the invited address and use the emailed link.
    async def emailed_token():
        async with client.app.state.db.session() as db:
            rows = (await db.scalars(select(MailOutbox).where(MailOutbox.email == "new@example.test"))).all()
            party = await db.scalar(select(AgreementParty).where(AgreementParty.email == "new@example.test"))
            assert len(rows) == 1 and party.account_id is None
            return rows[0].payload["token"]

    token = client.portal.call(emailed_token)
    post(client, "/agreement-invitations/respond", pt, {"token": token, "action": "accept"}, 403)  # wrong mailbox
    newcomer, nt = register(client, suffix="new")
    post(client, "/agreement-invitations/respond", nt, {"token": token, "action": "accept"})
    post(client, "/agreement-invitations/respond", nt, {"token": token, "action": "accept"}, 404)  # single use

    # Review, change request, revision (new immutable version), approvals.
    act(ut, "approve")
    act(pt, "request_changes", reason="Clarify the capital contribution")
    act(ut, "revise", 409, reason="Not the author", terms=TERMS)
    act(
        wt,
        "revise",
        reason="Capital clause clarified",
        terms={**TERMS, "obligations": "Each partner contributes PKR 500,000"},
    )
    detail = get(client, path, ut)
    assert detail["current_version"] == 2 and len(detail["versions"]) == 2
    assert all(p["approval"] == "PENDING" for p in detail["parties"])
    act(wt, "open_signing", 409)
    for approver in (ut, pt, nt):
        act(approver, "approve")
    assert get(client, path, wt)["status"] == "APPROVED"
    act(wt, "sign", 409)  # a drafting Wakeel is not a signatory
    act(wt, "open_signing")

    def sign(person, token, status=200, password="long-secure-password"):
        current = get(client, path, token)
        digest = next(v["digest"] for v in current["versions"] if v["number"] == current["current_version"])
        return act(
            token, "sign", status, consent=True, signed_name=person["name"], version_digest=digest, password=password
        )

    sign(user, ut, 403, password="wrong-password")
    assert sign(user, ut)["status"] == "PARTIALLY_SIGNED"
    sign(user, ut, 409)
    sign(partner, pt)
    final = sign(newcomer, nt)
    assert final["status"] == "ACTIVE" and final["active_version"] == 2
    detail = get(client, path, nt)
    signatures = [a for a in detail["acceptances"] if a["kind"] == "signature"]
    assert len(signatures) == 3 and all(a["version"] == 2 and a["evidence"]["method"] for a in signatures)
    assert len(detail["versions"]) == 2  # the first version is preserved
    act(wt, "revise", 409, reason="Too late to edit", terms=TERMS)
    assert (
        len(get(client, "/agreements", ut)) == 1 and get(client, "/agreements", register(client, suffix="z")[1]) == []
    )


def test_unknown_email_recovery_is_indistinguishable(platform, email_settings):
    register(platform)
    known = post(platform, "/auth/forgot-password", None, {"email": "a@example.test"})
    unknown = post(platform, "/auth/forgot-password", None, {"email": "nobody@example.test"})
    assert known["message"] == unknown["message"] and set(known) == set(unknown)
    for email, challenge in (("a@example.test", known), ("nobody@example.test", unknown)):
        body = {"email": email, "challenge_id": challenge["challenge_id"], "otp": "000000"}
        result = post(platform, "/auth/verify-otp", None, body, 400)
        assert result["code"] in {"otp_incorrect"} and result["attempts_remaining"] == 4

    async def decoys():
        async with platform.app.state.db.session() as db:
            return len((await db.scalars(select(OtpChallenge).where(OtpChallenge.user_id.is_(None)))).all())

    assert platform.portal.call(decoys) == 1
    post(platform, "/auth/resend-otp", None, {"email": "a@example.test", "challenge_id": known["challenge_id"]}, 429)


def test_documents_are_private_and_oversight_is_admin_only(platform):
    client = platform
    user, ut, wakeel, wt = people(client)
    outsider, ot = register(client, suffix="outsider")
    workspace = hire(client, ut, wt, wakeel)
    upload = request(
        client,
        "POST",
        f"/relationships/{workspace}/documents",
        ut,
        files={"file": ("deed.pdf", b"%PDF-1.4\nprivate", "application/pdf")},
        data={"title": "Sale deed", "kind": "evidence"},
    )
    assert upload.status_code == 201, upload.text
    document = upload.json()
    assert document["title"] == "Sale deed" and "content" not in document
    # A disguised executable is rejected by content, whatever the browser claims.
    bad = request(
        client,
        "POST",
        f"/relationships/{workspace}/documents",
        ut,
        files={"file": ("deed.pdf", b"MZ\x90\x00not a pdf", "application/pdf")},
    )
    assert bad.status_code == 415
    assert request(client, "GET", f"/documents/{document['id']}", wt).content.startswith(b"%PDF")
    assert request(client, "GET", f"/documents/{document['id']}", ot).status_code == 404
    assert get(client, "/documents", ot) == [] and len(get(client, "/documents", wt)) == 1
    assert (
        request(
            client, "POST", f"/relationships/{workspace}/documents", ot, files={"file": ("a.pdf", b"%PDF-1")}
        ).status_code
        == 404
    )
    # Oversight lists expose metadata only, to administrators only, and are audited.
    get(client, "/admin/records?kind=relationships", ut, 403)
    rows = get(client, "/admin/records?kind=hiring", admin_token(client))["rows"]
    assert rows and "description" not in rows[0]
    assert get(client, "/notifications/count", wt).keys() == {"unread", "messages", "hiring"}
