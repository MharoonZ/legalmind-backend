from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from app.schemas.qa import AskOut


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Login(Input):
    email: str = Field(max_length=254)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        import re

        value = value.casefold()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Enter a valid email address")
        return value


COMMON_PASSWORDS = frozenset(
    "123456 1234567 12345678 123456789 1234567890 password password1 password123 qwerty qwerty123 "
    "111111 000000 abc123 iloveyou admin admin123 letmein welcome pakistan pakistan123 legalmind".split()
)


def password_policy(value: str, *related: str) -> str:
    """Shared by registration, password change and reset. Never trust the browser's check."""
    from app.core.config import get_settings

    minimum = get_settings().password_min_length
    if len(value) < minimum:
        raise ValueError(f"Use at least {minimum} characters")
    lowered = value.casefold()
    if lowered in COMMON_PASSWORDS or len(set(value)) < 3:
        raise ValueError("This password is too common. Choose a stronger one")
    for item in related:
        if item and len(item) >= 4 and lowered == item.casefold():
            raise ValueError("Your password must not match your name or email address")
    return value


class Register(Login):
    name: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=6, max_length=128)

    @field_validator("password")
    @classmethod
    def strong_password(cls, value, info):
        email = info.data.get("email", "")
        return password_policy(value, email, email.split("@")[0])

    role: Literal["user", "wakeel"] = "user"
    bar_number: str = Field(default="", max_length=120)
    specialization: str = Field(default="", max_length=160)
    experience: int = Field(default=0, ge=0, le=70)
    city: str = Field(default="", max_length=120)

    @model_validator(mode="after")
    def professional_details(self):
        if self.role == "wakeel" and (not self.bar_number or not self.specialization or not self.city):
            raise ValueError("Wakeel registration requires bar number, specialization and city")
        return self


class Profile(Input):
    name: str = Field(min_length=2, max_length=120)
    city: str = Field(default="", max_length=120)
    biography: str = Field(default="", max_length=4000)
    specialization: str = Field(default="", max_length=160)
    experience: int = Field(default=0, ge=0, le=70)
    qualifications: str = Field(default="", max_length=2000)
    services: str = Field(default="", max_length=2000)
    fees: str = Field(default="", max_length=200)


class NewPassword(Input):
    password: str = Field(min_length=6, max_length=128)

    @field_validator("password")
    @classmethod
    def strong_password(cls, value):
        return password_policy(value)


class PasswordChange(NewPassword):
    current_password: str = Field(min_length=1, max_length=128)


class ResetPassword(NewPassword):
    token: str = Field(min_length=20, max_length=200)


class ChatAsk(Input):
    question: str = Field(min_length=1, max_length=4000)
    request_id: UUID
    law: str | None = Field(default=None, max_length=255)


class GuestTurn(Input):
    question: str = Field(min_length=1, max_length=4000)
    response: AskOut


class GuestImport(Input):
    messages: list[GuestTurn] = Field(min_length=1, max_length=20)


class StartConversation(Input):
    wakeel_id: UUID


class SendMessage(Input):
    body: str = Field(min_length=1, max_length=8000)
    request_id: UUID


class PreparationIn(Input):
    conversation_id: UUID
    title: str = Field(min_length=3, max_length=180)
    requirements: str = Field(min_length=10, max_length=12000)


class Terms(Input):
    title: str = Field(min_length=3, max_length=180)
    description: str = Field(min_length=10, max_length=5000)
    scope: str = Field(min_length=5, max_length=5000)
    deliverables: str = Field(min_length=3, max_length=5000)
    fees: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    currency: Literal["PKR", "USD"] = "PKR"
    payment_terms: str = Field(min_length=3, max_length=5000)
    start_date: date
    completion_date: date
    user_responsibilities: str = Field(min_length=3, max_length=5000)
    wakeel_responsibilities: str = Field(min_length=3, max_length=5000)
    conditions: str = Field(min_length=3, max_length=5000)
    required_documents: str = Field(default="", max_length=5000)
    other_terms: str = Field(default="", max_length=5000)

    @model_validator(mode="after")
    def dates(self):
        if self.completion_date < self.start_date:
            raise ValueError("Completion must be on or after the start date")
        return self


class CreateAgreement(Input):
    conversation_id: UUID
    preparation_id: UUID | None = None
    case_id: UUID | None = None
    terms: Terms


class AgreementAction(Input):
    action: Literal[
        "send",
        "review",
        "accept",
        "decline",
        "request_changes",
        "reject_changes",
        "revise",
        "sign",
        "amend",
        "withdraw",
        "request_closure",
        "confirm_closure",
        "dispute",
        "request_termination",
        "confirm_termination",
        "archive",
        # Multi-party agreements
        "approve",
        "reject",
        "open_signing",
        "resend_invitations",
    ]
    revision: int = Field(ge=1)
    reason: str = Field(default="", max_length=8000)
    terms: dict | None = None
    password: str = Field(default="", max_length=128)
    signed_name: str = Field(default="", max_length=120)
    consent: bool = False
    version_digest: str = Field(default="", max_length=64)


Money = Decimal
CASE_CATEGORIES = (
    "Property dispute",
    "Family dispute",
    "Divorce matter",
    "Criminal matter",
    "Civil dispute",
    "Inheritance dispute",
    "Employment dispute",
    "Business dispute",
    "Consumer complaint",
    "Money recovery",
    "Legal notice",
    "Other",
)
FeeType = Literal["FIXED", "HOURLY", "MILESTONE", "OTHER"]
Currency = Literal["PKR", "USD"]


def bounded_details(value: dict) -> dict:
    if len(value) > 24:
        raise ValueError("Too many additional fields")
    clean = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str) or not (0 < len(key) <= 40) or len(item) > 4000:
            raise ValueError("Additional fields must be short text values")
        if item.strip():
            clean[key] = item.strip()
    return clean


class CaseFields(Input):
    title: str = Field(min_length=3, max_length=180)
    description: str = Field(min_length=5, max_length=8000)
    category: str = Field(default="", max_length=60)
    subcategory: str = Field(default="", max_length=80)
    legal_issue: str = Field(default="", max_length=4000)
    priority: Literal["LOW", "NORMAL", "HIGH", "URGENT"] = "NORMAL"
    jurisdiction: str = Field(default="", max_length=160)
    court: str = Field(default="", max_length=160)
    reference_number: str = Field(default="", max_length=80)
    opposing_party: str = Field(default="", max_length=200)
    fee_type: FeeType = "FIXED"
    proposed_fee: Money | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    currency: Currency = "PKR"
    advance_payment: Money | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    payment_schedule: str = Field(default="", max_length=2000)
    expenses: str = Field(default="", max_length=2000)
    payment_notes: str = Field(default="", max_length=2000)
    proposed_start: date | None = None
    expected_completion: date | None = None
    important_date: date | None = None
    details: dict = Field(default_factory=dict)

    _details = field_validator("details")(bounded_details)

    @model_validator(mode="after")
    def consistent(self):
        if self.proposed_start and self.expected_completion and self.expected_completion < self.proposed_start:
            raise ValueError("Expected completion must be on or after the proposed start date")
        if (
            self.advance_payment is not None
            and self.proposed_fee is not None
            and self.advance_payment > self.proposed_fee
        ):
            raise ValueError("The advance payment cannot exceed the proposed fee")
        return self


class CaseIn(CaseFields):
    agreement_id: UUID | None = None
    wakeel_id: UUID | None = None
    relationship_id: UUID | None = None
    draft: bool = False

    @model_validator(mode="after")
    def proposal(self):
        if self.relationship_id and self.category not in CASE_CATEGORIES:
            raise ValueError("Choose a case category")
        return self


class CaseRevision(CaseFields):
    reason: str = Field(default="", max_length=2000)


class CaseAction(Input):
    action: Literal[
        "submit",
        "discuss",
        "accept",
        "reject",
        "request_changes",
        "cancel",
        "request_completion",
        "confirm_completion",
        "raise_concern",
        "close",
    ]
    reason: str = Field(default="", max_length=4000)


class FeeIn(Input):
    amount: Money = Field(ge=0, max_digits=12, decimal_places=2)
    fee_type: FeeType = "FIXED"
    currency: Currency = "PKR"
    advance: Money | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    schedule: str = Field(default="", max_length=2000)
    expenses: str = Field(default="", max_length=2000)
    note: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def advance_within_fee(self):
        if self.advance is not None and self.advance > self.amount:
            raise ValueError("The advance payment cannot exceed the fee")
        return self


class Decision(Input):
    action: Literal["accept", "reject", "confirm"]
    note: str = Field(default="", max_length=2000)


class PaymentIn(Input):
    amount: Money = Field(gt=0, max_digits=12, decimal_places=2)
    method: str = Field(default="", max_length=40)
    reference: str = Field(default="", max_length=120)
    note: str = Field(default="", max_length=2000)


class DeadlineIn(Input):
    title: str = Field(min_length=3, max_length=180)
    kind: Literal["hearing", "meeting", "deadline", "filing", "other"] = "deadline"
    due_on: date


class DeadlineState(Input):
    done: bool


class Score(Input):
    overall: int = Field(ge=1, le=5)
    body: str = Field(default="", max_length=3000)


class CaseRatingIn(Score):
    professionalism: int = Field(ge=1, le=5)
    communication: int = Field(ge=1, le=5)
    responsiveness: int = Field(ge=1, le=5)
    quality: int = Field(ge=1, le=5)


class RelationshipRatingIn(Score):
    professionalism: int = Field(ge=1, le=5)
    legal_knowledge: int = Field(ge=1, le=5)
    communication: int = Field(ge=1, le=5)
    timeliness: int = Field(ge=1, le=5)
    fee_transparency: int = Field(ge=1, le=5)
    satisfaction: int = Field(ge=1, le=5)


class HiringIn(Input):
    wakeel_id: UUID
    category: str = Field(min_length=2, max_length=80)
    requirements: str = Field(min_length=10, max_length=6000)
    description: str = Field(min_length=10, max_length=8000)
    communication: Literal["chat", "phone", "video", "in_person", "email"] = "chat"
    budget: Money | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    notes: str = Field(default="", max_length=4000)


class HiringAction(Input):
    action: Literal["discuss", "accept", "reject", "cancel"]
    reason: str = Field(default="", max_length=2000)


class RelationshipAction(Input):
    action: Literal["request_termination", "review_termination", "confirm_termination", "withdraw_termination"]
    reason: str = Field(default="", max_length=4000)


class PartyIn(Input):
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(max_length=254)
    phone: str = Field(default="", max_length=40)
    role: str = Field(min_length=2, max_length=60)
    organization: str = Field(default="", max_length=160)
    identification: str = Field(default="", max_length=120)
    address: str = Field(default="", max_length=1000)
    signatory: bool = True

    _email = field_validator("email")(Login.valid_email.__func__)


AGREEMENT_TYPES = (
    "Legal service agreement",
    "Partnership agreement",
    "Sale agreement",
    "Rental / tenancy agreement",
    "Employment agreement",
    "Non-disclosure agreement",
    "Loan agreement",
    "Settlement agreement",
    "Other",
)


class PartyTerms(Input):
    """Complete terms of one multi-party agreement version."""

    title: str = Field(min_length=3, max_length=180)
    agreement_type: str = Field(min_length=2, max_length=80)
    category: str = Field(default="", max_length=80)
    description: str = Field(min_length=10, max_length=5000)
    scope: str = Field(min_length=5, max_length=5000)
    obligations: str = Field(min_length=3, max_length=5000)
    deliverables: str = Field(default="", max_length=5000)
    fees: Money | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    currency: Currency = "PKR"
    payment_terms: str = Field(default="", max_length=5000)
    payment_schedule: str = Field(default="", max_length=5000)
    start_date: date | None = None
    effective_date: date
    expiry_date: date | None = None
    completion_date: date | None = None
    party_responsibilities: str = Field(default="", max_length=5000)
    wakeel_responsibilities: str = Field(default="", max_length=5000)
    conditions: str = Field(default="", max_length=5000)
    required_documents: str = Field(default="", max_length=5000)
    confidentiality: str = Field(default="", max_length=5000)
    termination: str = Field(min_length=3, max_length=5000)
    dispute_resolution: str = Field(min_length=3, max_length=5000)
    governing_law: str = Field(min_length=3, max_length=500)
    renewal: str = Field(default="", max_length=5000)
    other_terms: str = Field(default="", max_length=5000)

    @model_validator(mode="after")
    def dates(self):
        for name in ("expiry_date", "completion_date"):
            value = getattr(self, name)
            if value and value < self.effective_date:
                raise ValueError(name.replace("_", " ").capitalize() + " must be on or after the effective date")
        return self


class MultiPartyIn(Input):
    terms: PartyTerms
    parties: list[PartyIn] = Field(min_length=2, max_length=12)
    relationship_id: UUID | None = None

    @model_validator(mode="after")
    def distinct(self):
        emails = [party.email for party in self.parties]
        if len(set(emails)) != len(emails):
            raise ValueError("Each party needs a different email address")
        if sum(party.signatory for party in self.parties) < 2:
            raise ValueError("An agreement needs at least two signing parties")
        return self


class InvitationResponse(Input):
    token: str = Field(min_length=20, max_length=200)
    action: Literal["accept", "decline"]
    reason: str = Field(default="", max_length=2000)


class Announcement(Input):
    title: str = Field(min_length=5, max_length=160)
    audience: Literal["all", "user", "wakeel"] = "all"


class CaseNote(Input):
    note: str = Field(min_length=3, max_length=8000)
    status: Literal[
        "ACTIVE",
        "AWAITING_CLIENT_INFORMATION",
        "AWAITING_WAKEEL_ACTION",
        "DRAFT",
        "OPEN",
        "SUBMITTED",
        "UNDER_REVIEW",
        "ASSIGNED",
        "IN_PROGRESS",
        "AWAITING_HEARING",
        "HEARING_COMPLETED",
        "AWAITING_DECISION",
        "RESOLVED",
        "DISMISSED",
        "ON_HOLD",
        "CLOSED",
        "REOPENED",
    ]
    important_date: date | None = None


class ReviewIn(Input):
    agreement_id: UUID
    rating: int = Field(ge=1, le=5)
    body: str = Field(min_length=5, max_length=3000)


class ReportIn(Input):
    conversation_id: UUID
    reason: str = Field(min_length=10, max_length=8000)


class AdminAction(Input):
    action: Literal["verify", "reject", "suspend", "restore", "resolve_report", "resolve_dispute"]
    resource_id: UUID
    reason: str = Field(min_length=5, max_length=8000)
    outcome: Literal["ACTIVE", "CANCELLED"] | None = None


class EmailInput(Input):
    email: str = Field(max_length=254)
    _email = field_validator("email")(Login.valid_email.__func__)


class OtpVerify(EmailInput):
    challenge_id: UUID
    otp: str = Field(pattern=r"^\d{6}$")


class OtpResend(EmailInput):
    challenge_id: UUID


class EmailPreferences(Input):
    messages: bool = True
    agreements: bool = True
    cases: bool = True
    account: bool = True


class DraftIn(Input):
    preparation_id: UUID | None = None
    case_id: UUID | None = None
    revision: int = Field(ge=0)
    terms: dict[str, str] = Field(max_length=14)

    @field_validator("terms")
    @classmethod
    def validate_terms(cls, value):
        if set(value) - set(Terms.model_fields) or any(len(v) > 5000 for v in value.values()):
            raise ValueError("Invalid draft fields")
        return value


class CaseLink(Input):
    agreement_id: UUID


class CaseAssignment(Input):
    wakeel_id: UUID
    reason: str = Field(min_length=5, max_length=1000)


class PreparationAction(Input):
    action: Literal["accept", "decline"]
    reason: str = Field(min_length=5, max_length=1000)


def parse(model, data, *location):
    """Validate a nested payload inside a handler and report it like any other 422."""
    try:
        return model.model_validate(data)
    except ValidationError as error:
        raise RequestValidationError(
            [{**issue, "loc": ("body", *location, *issue["loc"])} for issue in error.errors(include_context=False)]
        ) from None
