"""Provider boundaries shared by the worker, fixture tests, and integrations."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel

ModelT = TypeVar("ModelT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class ResponseMetadata:
    """Normalized attribution and billing, independent of content acceptance."""

    requested_model: str
    resolved_model: str | None = None
    provider: str | None = None
    request_id: str | None = None
    usage: dict[str, Any] | None = None
    cost_usd: Decimal | None = None


class ResearchResults(list[dict[str, Any]]):
    """Search rows plus the billing receipt for this specific request."""

    def __init__(self, rows: list[dict[str, Any]], cost_micro_usd: int | None) -> None:
        super().__init__(rows)
        self.cost_micro_usd = cost_micro_usd


class DispatchAdmissionError(Exception):
    """Marker mixed into provider errors that must stop a whole collection."""


class ProviderError(Exception):
    """A public-safe classification without upstream bodies or credentials.

    The scheduler owns retry decisions. ``outcome_unknown`` means a request may
    have completed remotely; callers must retain/reconcile its reservation.
    """

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        retry_after: float | None = None,
        outcome_unknown: bool = False,
        response: ResponseMetadata | None = None,
        validation_kind: Literal["json", "schema", "semantics", "envelope"] | None = None,
        schema_name: str | None = None,
        output_truncated: bool = False,
        research_cost_micro: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after
        self.outcome_unknown = outcome_unknown
        self.response = response
        self.validation_kind = validation_kind
        self.schema_name = schema_name
        self.output_truncated = output_truncated
        self.research_cost_micro = research_cost_micro


@dataclass(frozen=True, slots=True)
class ModelResult[T: BaseModel]:
    data: T
    requested_model: str
    resolved_model: str
    request_id: str | None = None
    provider: str | None = None
    usage: dict[str, Any] | None = None
    cost_usd: Decimal | None = None
    duration_seconds: float = 0.0
    fixture: bool = False


class ModelGateway(Protocol):
    async def generate(
        self,
        schema: type[ModelT],
        messages: list[dict[str, str]],
        call_identity: str,
        max_tokens: int = 2048,
        *,
        reserved_cost_usd: Decimal | None = None,
    ) -> ModelResult[ModelT]: ...
