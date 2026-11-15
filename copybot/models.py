from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
Nonnegative = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
Fraction = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Trader(Model):
    wallet: str = Field(min_length=1)
    chain: Literal["solana"] = "solana"
    sources: list[str] = Field(min_length=1)
    allocation: Fraction
    reference_equity_usd: Positive
    enabled: bool = True


class Limits(Model):
    max_trade_usd: Positive = Decimal("50")
    max_token_fraction: Fraction = Decimal("0.15")
    max_total_fraction: Fraction = Decimal("0.70")
    daily_loss_fraction: Fraction = Decimal("0.03")
    min_trade_usd: Positive = Decimal("5")
    min_liquidity_usd: Positive = Decimal("100000")
    max_price_drift: Fraction = Decimal("0.02")
    max_signal_age_seconds: int = Field(default=90, gt=0)
    max_quote_age_seconds: int = Field(default=30, gt=0)
    fee_bps: Annotated[Decimal, Field(ge=0, lt=10000)] = Decimal("30")
    slippage_bps: Annotated[Decimal, Field(ge=0, lt=10000)] = Decimal("50")
    stop_loss_fraction: Annotated[Decimal, Field(gt=0, lt=1)] = Decimal("0.15")


class Settings(Model):
    mode: Literal["paper"] = "paper"
    initial_cash_usd: Positive = Decimal("1000")
    poll_seconds: int = Field(default=15, ge=5)
    advisor: Literal["rules", "openai"] = "rules"
    advisor_ttl_seconds: int = Field(default=120, ge=5, le=300)
    limits: Limits = Field(default_factory=Limits)
    traders: list[Trader] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_roster(self):
        wallets = [t.wallet for t in self.traders]
        if len(set(wallets)) != len(wallets):
            raise ValueError("Duplicate wallet: combine its sources into one trader")
        if sum(t.allocation for t in self.traders if t.enabled) > 1:
            raise ValueError("Enabled trader allocations exceed 100%")
        if self.limits.min_trade_usd > self.limits.max_trade_usd:
            raise ValueError("Minimum trade exceeds maximum trade")
        return self


class Trade(Model):
    # Birdeye is the sole execution signal feed. Discovery sources never submit orders.
    event_id: str = Field(min_length=1)
    wallet: str = Field(min_length=1)
    token: str = Field(min_length=1)
    side: Literal["buy", "sell"]
    quantity: Positive
    price_usd: Positive
    timestamp: int = Field(gt=0)
    leader_balance_before: Nonnegative | None = None


class Quote(Model):
    token: str
    price_usd: Positive
    liquidity_usd: Nonnegative
    timestamp: int = Field(gt=0)


class Assessment(Model):
    multiplier: Fraction
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Reason must not be blank")
        return value


class Candidate(Model):
    wallet: str = Field(min_length=1)
    chain: Literal["solana"] = "solana"
    sources: list[str] = Field(min_length=1)
    evidence: dict = Field(default_factory=dict)
