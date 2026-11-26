from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
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


class Register(Login):
    name: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=6, max_length=128)
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


class PasswordChange(Input):
    current_password: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=6, max_length=128)


class ResetPassword(Input):
    token: str = Field(min_length=20, max_length=200)
    password: str = Field(min_length=6, max_length=128)


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
    ]
    revision: int = Field(ge=1)
    reason: str = Field(default="", max_length=8000)
    terms: Terms | None = None
    signed_name: str = Field(default="", max_length=120)
    consent: bool = False
    version_digest: str = Field(default="", max_length=64)


class CaseIn(Input):
    agreement_id: UUID
    title: str = Field(min_length=3, max_length=180)
    description: str = Field(min_length=5, max_length=8000)
    important_date: date | None = None


class CaseNote(Input):
    note: str = Field(min_length=3, max_length=8000)
    status: Literal["OPEN", "IN_PROGRESS", "ON_HOLD", "CLOSED"]
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
