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


class Discovery(Contract):
    entity: Identifier
    name: Annotated[str, Field(min_length=1, max_length=120)]
    mechanism: Text
    observed_identifiers: Annotated[list[Text], Field(min_length=1, max_length=12)]
    sources: Annotated[list[Text], Field(min_length=1, max_length=6)]
    instruments: Annotated[list[Identifier], Field(max_length=12)]


class ImplementationCheck(Contract):
    kind: Literal["spot", "perp", "hip3", "prediction"]
    instrument_id: Identifier | None = None
    status: Literal["viable", "rejected", "not_found", "incompatible", "unverified"]
    reason: Text
    # Human-readable observations; independent reads establish IDs, not conclusions.
    observations: Annotated[list[Text], Field(max_length=6)] = []

    @model_validator(mode="after")
    def validate_implementation(self) -> Self:
        if self.status in {"viable", "rejected"} and not self.instrument_id:
            raise ValueError("A compared implementation requires its observed ID")
        return self


class DiscoveryDisposition(Contract):
    entities: Annotated[list[Identifier], Field(min_length=1, max_length=120)]
    status: Literal["assessed", "out_of_scope", "needs_evidence"]
    candidate_entity: Identifier | None = None
    reason: Text

    @model_validator(mode="after")
    def validate_link(self) -> Self:
        if (self.status == "assessed") != (self.candidate_entity is not None):
            raise ValueError("Only assessed discoveries link to a candidate_entity")
        return self


CaseBasis = Literal["economic", "narrative", "mixed", "hedge", "event"]


class CaseResearch(Discovery):
    effect_order: Literal[1, 2, 3]
    value_capture: Text
    support: Text
    counterevidence: Text
    closest_alternative: Text
    gaps: Annotated[list[Text], Field(max_length=6)]


class ResearchCase(CaseResearch):
    """A ranked comparison, not the parent's investment decision."""

    case_basis: CaseBasis


class CandidateCase(CaseResearch):
    decision: Literal["KEEP", "ALTERNATIVE", "REJECT", "NEEDS_EVIDENCE"]
    reason: Text
    decision_basis: (
        Literal["economic", "implementation", "portfolio", "unresolved"] | None
    ) = None
    implementation_checks: Annotated[
        list[ImplementationCheck], Field(max_length=12)
    ] = []
    # Historical receipts predate an explicit investment basis.
    case_basis: CaseBasis | None = None


class ResearchCheckpoint(Contract):
    schema_version: Literal[1, 2, 3] = 1
    stage: Literal["interpretation", "discovery", "provisional", "judged"]
    spec: ThesisSpec
    discoveries: Annotated[list[Discovery], Field(max_length=120)] = []
    discovery_dispositions: Annotated[
        list[DiscoveryDisposition], Field(max_length=480)
    ] = []
    candidates: Annotated[list[CandidateCase], Field(max_length=120)] = []
    proposal: Proposal | None = None
    research_cases: Annotated[list[ResearchCase], Field(max_length=10)] = []

    @model_validator(mode="after")
    def validate_checkpoint(self) -> Self:
        entities = [case.entity.casefold() for case in self.candidates]
        if len(set(entities)) != len(entities):
            raise ValueError("Group implementations of the same economic entity")
        dispositions = [
            e.casefold() for d in self.discovery_dispositions for e in d.entities
        ]
        if len(dispositions) != len(set(dispositions)):
            raise ValueError("Each discovery key has one disposition")
        if self.schema_version >= 2 and self.stage == "judged":
            if not self.candidates and not self.discovery_dispositions:
                raise ValueError(
                    "A judged checkpoint must retain the assessment ledger or update dispositions"
                )
            for case in self.candidates:
                if self.schema_version == 3 and case.case_basis is None:
                    raise ValueError(f"{case.entity}: specify case_basis")
                if case.decision_basis is None:
                    raise ValueError(
                        f"{case.entity}: specify the exposure decision_basis"
                    )
                if (
                    case.decision_basis == "unresolved"
                    and case.decision != "NEEDS_EVIDENCE"
                ):
                    raise ValueError(
                        f"{case.entity}: unresolved is NEEDS_EVIDENCE, not rejection"
                    )
                if case.decision_basis == "implementation" and case.decision in {
                    "REJECT",
                    "ALTERNATIVE",
                }:
                    checks = case.implementation_checks
                    alternatives = {(c.kind, c.instrument_id) for c in checks}
                    if len(alternatives) < 2 or any(
                        c.status == "unverified" for c in checks
                    ):
                        raise ValueError(
                            f"{case.entity}: compare alternative implementations before "
                            "dropping the exposure; unresolved alternatives are NEEDS_EVIDENCE"
                        )
                    if any(c.status == "viable" for c in checks):
                        raise ValueError(
                            f"{case.entity}: a viable alternative remains; implementation "
                            "failure alone cannot exclude this exposure"
                        )
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


class DiscoveryCheckpoint(Contract):
    """Separate worker input: judgments/portfolios are structurally impossible."""

    schema_version: Literal[3] = 3
    stage: Literal["discovery"] = "discovery"
    spec: ThesisSpec
    discoveries: Annotated[list[Discovery], Field(max_length=120)] = []
    research_cases: Annotated[list[ResearchCase], Field(max_length=10)] = []
