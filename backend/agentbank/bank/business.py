from __future__ import annotations

import uuid
import json
from dataclasses import dataclass
from datetime import date
from calendar import monthrange

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentbank.clock import BusinessClock, business_date
from agentbank.db import Account, Card, Database, Event, Mandate, Payee, Product, Reservation, RiskProfile, Subscription
from agentbank.errors import AmbiguousRecipient, DomainError
from agentbank.db import RiskAssessment
from agentbank.bank.questionnaire import VERSION
from agentbank.bank.ledger import require_active


@dataclass(frozen=True)
class RecipientCandidate:
    id: str
    masked_name: str
    account_id: str
    is_new: bool
    phone_tail: str | None = None


class RecipientResolver:
    def __init__(self, database: Database) -> None:
        self.database = database

    def by_name(self, user_id: str, name: str) -> RecipientCandidate:
        with self.database.sessions() as session:
            payees = list(session.scalars(select(Payee).where(Payee.owner_user_id == user_id, Payee.display_name == name)).all())
        if not payees:
            raise DomainError("RECIPIENT_NOT_FOUND")
        if len(payees) > 1:
            raise AmbiguousRecipient()
        return self._candidate(payees[0])

    def by_phone(self, user_id: str, phone: str) -> RecipientCandidate:
        with self.database.sessions() as session:
            payee = session.scalar(select(Payee).where(Payee.owner_user_id == user_id, Payee.phone == phone))
        if payee is None:
            raise DomainError("RECIPIENT_NOT_FOUND")
        return self._candidate(payee)

    @staticmethod
    def _candidate(payee: Payee) -> RecipientCandidate:
        return RecipientCandidate(payee.id, payee.masked_name, payee.account_id, payee.is_new,
                                  payee.phone[-4:] if payee.phone else None)


@dataclass(frozen=True)
class RiskProfileView:
    level: str
    score: int
    expires_on: str


class RiskService:
    def __init__(self, database: Database, clock: BusinessClock | None = None) -> None:
        self.database = database
        self.clock = clock or BusinessClock()

    def submit(self, user_id: str, answers: list[int], questionnaire_version: str = VERSION) -> RiskProfileView:
        if questionnaire_version != VERSION or len(answers) != 10 or any(type(answer) is not int or answer < 0 or answer > 5 for answer in answers):
            raise DomainError("INVALID_RISK_QUESTIONNAIRE")
        score = sum(answers)
        level = "C1" if score <= 10 else "C2" if score <= 20 else "C3" if score <= 30 else "C4" if score <= 40 else "C5"
        today = date.fromisoformat(self.clock.business_date())
        expires = today.replace(year=today.year + 1, day=min(today.day, monthrange(today.year + 1, today.month)[1])).isoformat()
        with self.database.sessions.begin() as session:
            session.add(RiskAssessment(id=f'risk_{uuid.uuid4().hex}', user_id=user_id,
                questionnaire_version=questionnaire_version, answers_json=json.dumps(answers),
                score=score, level=level, assessed_on=today.isoformat(), expires_on=expires))
            profile = session.get(RiskProfile, user_id)
            if profile is None:
                profile = RiskProfile(user_id=user_id, level=level, score=score, expires_on=expires)
                session.add(profile)
            else:
                profile.level, profile.score, profile.expires_on = level, score, expires
        return RiskProfileView(level, score, expires)

    def recommend(self, user_id: str) -> list[Product]:
        with self.database.sessions() as session:
            profile = session.get(RiskProfile, user_id)
            if profile is None:
                raise DomainError("RISK_PROFILE_REQUIRED")
            if profile.expires_on < business_date():
                raise DomainError("RISK_PROFILE_EXPIRED")
            max_level = int(profile.level[1:])
            return list(session.scalars(select(Product).where(Product.risk_level <= f"R{max_level}")).all())


class CardService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def freeze(self, user_id: str, card_id: str | None = None) -> str:
        with self.database.sessions.begin() as session:
            card = self._get(session, user_id, card_id)
            if card.status == "active":
                card.status = "frozen"
            return card.id

    def unfreeze(self, user_id: str, card_id: str, *, assurance: str) -> None:
        if assurance != "mfa":
            raise DomainError("MFA_REQUIRED")
        with self.database.sessions.begin() as session:
            card = self._get(session, user_id, card_id)
            if card.status != "frozen":
                raise DomainError("INVALID_CARD_STATE")
            card.status = "active"

    def status(self, card_id: str) -> str:
        with self.database.sessions() as session:
            card = session.get(Card, card_id)
            if card is None:
                raise DomainError("CARD_NOT_FOUND")
            return card.status

    @staticmethod
    def _get(session: Session, user_id: str, card_id: str | None) -> Card:
        card = session.get(Card, card_id) if card_id else session.scalar(select(Card).where(Card.user_id == user_id).order_by(Card.id))
        if card is None or card.user_id != user_id:
            raise DomainError("CARD_NOT_FOUND")
        return card


class SubscriptionService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def list(self, user_id: str) -> list[Subscription]:
        with self.database.sessions() as session:
            return list(session.scalars(select(Subscription).where(Subscription.user_id == user_id)).all())

    def get(self, subscription_id: str) -> Subscription:
        with self.database.sessions() as session:
            subscription = session.get(Subscription, subscription_id)
            if subscription is None:
                raise DomainError("SUBSCRIPTION_NOT_FOUND")
            return Subscription(id=subscription.id, user_id=subscription.user_id, merchant=subscription.merchant,
                                amount_minor=subscription.amount_minor, status=subscription.status,
                                next_charge_at=subscription.next_charge_at, mandate_id=subscription.mandate_id)

    def cancel(self, user_id: str, subscription_id: str) -> None:
        with self.database.sessions.begin() as session:
            subscription = session.get(Subscription, subscription_id)
            if subscription is None or subscription.user_id != user_id:
                raise DomainError("NOT_OWNER")
            subscription.status = "cancelled"
            subscription.mandate_id = None

    def should_charge(self, subscription_id: str, on_date: date) -> bool:
        subscription = self.get(subscription_id)
        return subscription.status == "active" and date.fromisoformat(subscription.next_charge_at) <= on_date


@dataclass(frozen=True)
class MandateView:
    id: str
    max_amount_minor: int
    max_uses: int


class MandateService:
    def __init__(self, database: Database, clock: BusinessClock) -> None:
        self.database, self.clock = database, clock

    def create(self, user_id: str, plan_id: str, operation_type: str, subject_ref: str, max_amount_minor: int,
               max_uses: int, starts_on: date, expires_on: date) -> MandateView:
        mandate = Mandate(id=f"mdt_{uuid.uuid4().hex}", user_id=user_id, plan_id=plan_id,
                          operation_type=operation_type, subject_ref=subject_ref, max_amount_minor=max_amount_minor,
                          max_uses=max_uses, starts_on=starts_on.isoformat(), expires_on=expires_on.isoformat())
        with self.database.sessions.begin() as session:
            session.add(mandate)
        return MandateView(mandate.id, max_amount_minor, max_uses)

    def consume(self, mandate_id: str, amount_minor: int) -> bool:
        with self.database.sessions.begin() as session:
            mandate = session.get(Mandate, mandate_id)
            if mandate is None or mandate.status != "active":
                return False
            today = date.fromisoformat(self.clock.business_date())
            if not (date.fromisoformat(mandate.starts_on) <= today <= date.fromisoformat(mandate.expires_on)):
                return False
            if amount_minor > mandate.max_amount_minor or mandate.used_count >= mandate.max_uses:
                return False
            mandate.used_count += 1
            return True


class BirthdayPlanner:
    def __init__(self, database: Database, clock: BusinessClock, mandates: MandateService) -> None:
        self.database, self.clock, self.mandates = database, clock, mandates

    def discover(self, user_id: str) -> list[dict[str, str]]:
        with self.database.sessions() as session:
            events = list(session.scalars(select(Event).where(Event.user_id == user_id, Event.event_type == "birthday")).all())
        return [{"event_id": event.id, "title": event.title, "event_date": event.event_date} for event in events]

    def reserve(self, user_id: str, amount_minor: int, plan_id: str) -> str:
        if amount_minor <= 0:
            raise DomainError("INVALID_AMOUNT")
        with self.database.sessions.begin() as session:
            account = session.scalar(select(Account).where(Account.user_id == user_id).order_by(Account.id))
            account = require_active(account, user_id)
            if account.available_minor < amount_minor:
                raise DomainError("INSUFFICIENT_FUNDS")
            account.available_minor -= amount_minor
            reservation = Reservation(id=f"rsv_{uuid.uuid4().hex}", user_id=user_id, account_id=account.id,
                                      amount_minor=amount_minor, status="active", plan_id=plan_id)
            session.add(reservation)
            return reservation.id

    def release(self, user_id: str, reservation_id: str) -> None:
        with self.database.sessions.begin() as session:
            reservation = session.get(Reservation, reservation_id)
            if reservation is None or reservation.user_id != user_id:
                raise DomainError("NOT_OWNER")
            if reservation.status != "active":
                return
            account = session.get(Account, reservation.account_id)
            if account is None:
                raise DomainError("ACCOUNT_NOT_FOUND")
            account.available_minor += reservation.amount_minor
            reservation.status = "released"
