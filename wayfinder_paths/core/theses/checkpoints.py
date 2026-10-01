"""Untrusted research progress, separate from observed market evidence and orders."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from wayfinder_paths.core.theses.models import Contract, Identifier, Proposal, Text
from wayfinder_paths.core.theses.research import validate_full_allocation


class ThesisSpec(Contract):
    objective: Text
    horizon: Text
    constraints: Annotated[list[Text], Field(max_length=8)]
    mechanisms: Annotated[list[Text], Field(min_length=1, max_length=6)]
    causal_chain: Text
    counterfactual: Text
    baseline: Text


class CandidateCase(Contract):
    entity: Identifier
    name: Annotated[str, Field(min_length=1, max_length=120)]
    mechanism: Text
    effect_order: Literal[1, 2, 3]
    value_capture: Text
    support: Text
    counterevidence: Text
    closest_alternative: Text
    # Exact source-returned name/ID plus URLs allow evaluation against transcripts.
    observed_identifiers: Annotated[list[Text], Field(min_length=1, max_length=6)]
    sources: Annotated[list[Text], Field(min_length=1, max_length=6)]
    instruments: Annotated[list[Identifier], Field(max_length=6)]
    decision: Literal["KEEP", "ALTERNATIVE", "REJECT", "NEEDS_EVIDENCE"]
    reason: Text
    gaps: Annotated[list[Text], Field(max_length=6)]


class ResearchCheckpoint(Contract):
    stage: Literal["interpretation", "discovery", "provisional", "judged"]
    spec: ThesisSpec
    candidates: Annotated[list[CandidateCase], Field(max_length=60)] = []
    proposal: Proposal | None = None

    @model_validator(mode="after")
    def validate_checkpoint(self) -> Self:
        entities = [case.entity.casefold() for case in self.candidates]
        if len(set(entities)) != len(entities):
            raise ValueError("Group implementations of the same economic entity")
        if self.stage == "provisional" and self.proposal is None:
            raise ValueError("A provisional checkpoint requires a complete draft")
        if (
            self.stage == "provisional"
            and self.proposal is not None
            and not any(v.positions for v in self.proposal.variants)
        ):
            raise ValueError(
                "Not constructed is not a provisional investment portfolio"
            )
        if self.proposal is not None:
            validate_full_allocation(self.proposal)
        return self
