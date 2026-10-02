"""Untrusted research progress, separate from observed market evidence and orders."""

from typing import Annotated, Any, Literal, Self

from pydantic import Field, model_validator

from wayfinder_paths.core.theses.models import (
    Construction,
    Contract,
    DraftUpdate,
    Identifier,
    Proposal,
    Text,
)
from wayfinder_paths.core.theses.research import validate_full_allocation
from wayfinder_paths.core.theses.sizing import construction_errors


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


class CaseReference(Contract):
    session_id: Identifier
    checkpoint_id: Identifier
    entity: Identifier


class DecisionClaim(Contract):
    """A claim to review against saved reads, not a verified fact."""

    statement: Text
    basis: Literal["observation", "inference"]
    scope: Text
    evidence_part_ids: Annotated[list[Identifier], Field(min_length=1, max_length=3)]


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
    claims: Annotated[list[DecisionClaim], Field(max_length=4)] = []
    comparison_refs: Annotated[list[CaseReference], Field(max_length=3)] = []


class CaseDecision(Contract):
    """Parent judgment over immutable research; changes never overwrite its source."""

    research_ref: CaseReference
    entity: Identifier
    decision: Literal["KEEP", "ALTERNATIVE", "REJECT", "NEEDS_EVIDENCE"]
    decision_basis: Literal["economic", "implementation", "portfolio", "unresolved"]
    reason: Text
    implementation_checks: list[ImplementationCheck] = []
    updated_research: ResearchCase | None = None
    claims: Annotated[list[DecisionClaim], Field(max_length=4)] = []
    comparison_refs: Annotated[list[CaseReference], Field(max_length=3)] = []


class Handoff(Contract):
    case_entities: list[Identifier]
    unresolved_entities: list[Identifier]
    reason: Text


class HandoffGap(Contract):
    session_id: Identifier
    reason: Text


class ReviewFinding(Contract):
    id: Identifier
    entity: Identifier
    blocking: bool
    issue: Text
    required_change: Text


class ReviewCheckpoint(Contract):
    findings: Annotated[list[ReviewFinding], Field(max_length=24)]
    reviewed_revision: Identifier | None = None

    def receipt_json(self) -> str:
        # Preserve receipts from reviewers predating revision-bound sign-off.
        return self.model_dump_json(
            exclude={"reviewed_revision"} if self.reviewed_revision is None else set()
        )

    @model_validator(mode="after")
    def unique_findings(self) -> Self:
        if len({f.id for f in self.findings}) != len(self.findings):
            raise ValueError("Review finding IDs must be unique")
        return self


class ReviewResolution(Contract):
    review_session_id: Identifier
    finding_id: Identifier
    action: Literal["evidence", "changed", "removed", "accepted"]
    reason: Text
    # IDs of successful public tool parts, not model-authored citations.
    evidence_part_ids: list[Identifier] = []


V5_FIELDS = {"decisions", "handoff", "handoff_gaps", "review_resolutions"}


class ResearchCheckpoint(Contract):
    schema_version: Annotated[
        Literal[1, 2, 3, 4, 5, 6, 7],
        Field(
            description="Set 7 on every current parent write; omission means legacy v1, not the previous call's version."
        ),
    ] = 1
    stage: Annotated[
        Literal["interpretation", "discovery", "provisional", "judged", "draft"],
        Field(
            description="Required on every write, including corrections. Use draft for a draft update; this header is not inherited."
        ),
    ]
    spec: ThesisSpec | None = None
    discoveries: Annotated[list[Discovery], Field(max_length=120)] = []
    discovery_dispositions: Annotated[
        list[DiscoveryDisposition], Field(max_length=480)
    ] = []
    candidates: Annotated[list[CandidateCase], Field(max_length=120)] = []
    proposal: Proposal | None = None
    research_cases: Annotated[list[ResearchCase], Field(max_length=10)] = []
    construction: Construction | None = None
    draft: DraftUpdate | None = None
    decisions: Annotated[list[CaseDecision], Field(max_length=12)] = []
    handoff: Handoff | None = None
    handoff_gaps: list[HandoffGap] = []
    review_resolutions: list[ReviewResolution] = []

    @model_validator(mode="before")
    @classmethod
    def require_draft_headers(cls, value: Any) -> Any:
        # Otherwise a missing stage hides the legacy-version error until a retry.
        # Both forms already fail validation; do not infer or repair the payload.
        if (
            isinstance(value, dict)
            and value.get("draft") is not None
            and ("stage" not in value or "schema_version" not in value)
        ):
            raise ValueError(
                'Draft writes need both checkpoint.stage="draft" and explicit '
                "checkpoint.schema_version (use 7 for current parent research). "
                "They are not inherited from earlier writes. Retry the same draft "
                'inside {"checkpoint":{"schema_version":7,"stage":"draft",'
                '"draft":...}}; do not repeat the interpretation or other budgets.'
            )
        return value

    def receipt_json(self) -> str:
        # Preserve bytes of v3 receipts; v1/v2 compatibility lives in the reader.
        excluded: dict[str, Any] = {
            key: True for key in V5_FIELDS if self.schema_version < 5
        }
        if self.schema_version < 4:
            excluded.update(construction=True, draft=True)
        if self.schema_version < 7:
            for field in ("candidates", "decisions"):
                if field not in excluded:
                    excluded[field] = {"__all__": {"claims", "comparison_refs"}}
        return self.model_dump_json(exclude=excluded)

    @model_validator(mode="after")
    def validate_checkpoint(self) -> Self:
        errors = []
        if self.spec is None and (
            self.schema_version < 4 or self.stage == "interpretation"
        ):
            errors.append("An interpretation (and legacy checkpoint) requires spec")
        if self.schema_version < 4 and (
            self.construction is not None
            or self.draft is not None
            or self.stage == "draft"
        ):
            errors.append(
                "Construction and incremental drafts require schema_version>=4"
            )
        if self.schema_version < 5 and any(getattr(self, key) for key in V5_FIELDS):
            errors.append("Compact decisions and handoffs require schema_version>=5")
        if self.handoff is not None and self.stage != "discovery":
            errors.append("Only discovery checkpoints contain a worker handoff")
        if self.decisions and self.stage != "judged":
            errors.append("Compact decisions belong in judged checkpoints")
        assessments: list[CandidateCase | CaseDecision] = [
            *self.candidates,
            *self.decisions,
        ]
        for assessment in assessments:
            if self.schema_version < 7 and (
                assessment.claims or assessment.comparison_refs
            ):
                errors.append("Decision claims/comparisons require schema_version=7")
            if (
                self.schema_version >= 7
                and assessment.decision != "NEEDS_EVIDENCE"
                and not assessment.claims
            ):
                errors.append(
                    f"{assessment.entity}: attach 1-4 decisive claims with saved public "
                    "evidence_part_ids; missing proof is NEEDS_EVIDENCE, not rejection"
                )
            if any(
                ref.entity.casefold() == assessment.entity.casefold()
                for ref in assessment.comparison_refs
            ):
                errors.append(
                    f"{assessment.entity}: a comparison must reference another entity"
                )
        if self.schema_version >= 4:
            if (
                self.stage != "discovery"
                and (self.schema_version < 6 or self.stage == "interpretation")
                and self.construction is None
            ):
                errors.append("Parent checkpoints require the inferred construction")
            if (self.stage == "draft") != (self.draft is not None):
                errors.append("Only draft checkpoints contain a draft update")
            if self.stage == "draft" and self.proposal is not None:
                errors.append("Record a small draft update, not a complete proposal")
            if self.draft and self.construction:
                if (
                    self.draft.metadata
                    and self.draft.metadata.intent != self.construction.intent
                ):
                    errors.append(
                        "Proposal intent conflicts with the inferred construction"
                    )
                if self.draft.variant:
                    errors.extend(
                        construction_errors(self.draft.variant, self.construction)
                    )
        entities = [case.entity.casefold() for case in self.candidates] + [
            decision.entity.casefold() for decision in self.decisions
        ]
        if len(set(entities)) != len(entities):
            errors.append("Group implementations of the same economic entity")
        dispositions = [
            e.casefold() for d in self.discovery_dispositions for e in d.entities
        ]
        if len(dispositions) != len(set(dispositions)):
            errors.append("Each discovery key has one disposition")
        if self.schema_version >= 2 and self.stage == "judged":
            if (
                not self.candidates
                and not self.discovery_dispositions
                and not self.decisions
                and not self.review_resolutions
            ):
                errors.append(
                    "A judged checkpoint must retain the assessment ledger or update dispositions"
                )
            for case in self.candidates:
                if self.schema_version >= 3 and case.case_basis is None:
                    errors.append(f"{case.entity}: specify case_basis")
                if case.decision_basis is None:
                    errors.append(f"{case.entity}: specify the exposure decision_basis")
                if (
                    case.decision_basis == "unresolved"
                    and case.decision != "NEEDS_EVIDENCE"
                ):
                    errors.append(
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
                        errors.append(
                            f"{case.entity}: compare alternative implementations before "
                            "dropping the exposure; unresolved alternatives are NEEDS_EVIDENCE"
                        )
                    if any(c.status == "viable" for c in checks):
                        errors.append(
                            f"{case.entity}: a viable alternative remains; implementation "
                            "failure alone cannot exclude this exposure"
                        )
        if self.stage == "provisional" and self.proposal is None:
            errors.append("A provisional checkpoint requires a complete draft")
        if (
            self.stage == "provisional"
            and self.proposal is not None
            and not any(v.positions for v in self.proposal.variants)
        ):
            errors.append("Not constructed is not a provisional investment portfolio")
        if self.proposal is not None:
            if self.construction:
                for variant in self.proposal.variants:
                    errors.extend(
                        construction_errors(
                            variant, self.construction, intent=self.proposal.intent
                        )
                    )
            try:
                validate_full_allocation(self.proposal)
            except ValueError as exc:
                errors.append(str(exc))
        if errors:
            raise ValueError("\n".join(errors))
        return self


class DiscoveryCheckpoint(Contract):
    """Separate worker input: judgments/portfolios are structurally impossible."""

    schema_version: Literal[3, 5, 6] = 5
    stage: Literal["discovery"] = "discovery"
    spec: ThesisSpec | None = None
    discoveries: Annotated[list[Discovery], Field(max_length=120)] = []
    research_cases: Annotated[list[ResearchCase], Field(max_length=10)] = []
    handoff: Handoff | None = None

    @model_validator(mode="after")
    def require_legacy_spec(self) -> Self:
        if self.schema_version < 5 and self.spec is None:
            raise ValueError("Legacy discovery checkpoints require spec")
        return self
