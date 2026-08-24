"""The vocabularies the schema and the transition rules share.

These enums mirror the CHECK constraints in migrations/0001_initial.sql. They
are string enums so that a member can be handed to psycopg directly and read
back out of a row without conversion.
"""

from enum import StrEnum


class MemoryType(StrEnum):
    """Specification 13. State is the current state of a scope (14)."""

    OBSERVATION = "observation"
    FACT = "fact"
    INTERPRETATION = "interpretation"
    HYPOTHESIS = "hypothesis"
    DECISION = "decision"
    TASK = "task"
    PREFERENCE = "preference"
    STATE = "state"


class VersionStatus(StrEnum):
    """Specification 11.

    There is deliberately no active member. Whether a version is active is read
    off memory_entity.active_version and nowhere else (specification 10).

    rejected is the only member that is not a reading of the content. It says
    the proposal that carried this version was turned down, which is separate
    from whether the content might one day be worth revisiting; that question
    is what dormant answers.
    """

    CANDIDATE = "candidate"
    SUPERSEDED = "superseded"
    DISPROVEN = "disproven"
    DORMANT = "dormant"
    REJECTED = "rejected"
    COMPLETED = "completed"


class Delivery(StrEnum):
    """Where a memory goes, as opposed to what it is (specification 21.2).

    Bootstrap used to push every active preference and every current state, so
    a fixed per-session cost grew with the store. Delivery is the axis that was
    missing: being a preference says what the knowledge is, not that every
    session needs it in front of it.
    """

    #: Pushed at session start, before anything is known about the session.
    STARTUP_REQUIRED = "startup_required"
    #: Pushed once the session knows which scopes it is working in.
    SCOPE_REQUIRED = "scope_required"
    #: Never pushed. Reachable by search and by memory_get.
    PULL_ONLY = "pull_only"


class SourceType(StrEnum):
    """Where the content came from. Folded into the version row (specification 9)."""

    USER = "user"
    AGENT = "agent"
    TOOL = "tool"
    FILE = "file"
    WEB = "web"


class EntityStatus(StrEnum):
    """Specification 20.1. Separate from the status a version carries.

    provisional is what an entity holds when it was created despite a title
    similarity above the threshold: it exists and can hold versions, so the
    agent is not blocked, but it stays out of layer 1 of retrieval so that one
    concept never has two entities answering as current.
    """

    ACTIVE = "active"
    PROVISIONAL = "provisional"
    MERGED = "merged"
    ARCHIVED = "archived"


class ScopeStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ProposalOperation(StrEnum):
    """Specification 15."""

    CREATE = "create"
    UPDATE_VERSION = "update_version"
    CHANGE_STATUS = "change_status"
    RESTORE = "restore"
    MERGE = "merge"
    RETYPE = "retype"


class ProposalStatus(StrEnum):
    """Specification 15."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    AUTO_COMMITTED = "auto_committed"


class ConflictStatus(StrEnum):
    """Specification 23. Recorded by hand; never resolved automatically."""

    PENDING = "pending"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class EventType(StrEnum):
    """What the append-only log records (specification 26).

    Anything the system does that changes the knowledge state, or that hands
    context to an agent, leaves one row here.
    """

    SCOPE_CREATED = "scope_created"
    SCOPE_READINESS_SET = "scope_readiness_set"
    SCOPE_PROMOTED = "scope_promoted"
    DELIVERY_SET = "delivery_set"
    DIRECTIVE_SET = "directive_set"
    ENTITY_CREATED = "entity_created"
    ENTITY_MERGED = "entity_merged"
    TYPE_CORRECTED = "type_corrected"
    VERSION_CREATED = "version_created"
    STATUS_CHANGED = "status_changed"
    ACTIVE_SWITCHED = "active_switched"
    EVIDENCE_RECORDED = "evidence_recorded"
    CONTEXT_RECORDED = "context_recorded"
    CONTEXT_REVOKED = "context_revoked"
    PROPOSAL_CREATED = "proposal_created"
    PROPOSAL_DEFERRED = "proposal_deferred"
    EXTRACTION_FILED = "extraction_filed"
    PROPOSAL_COMMITTED = "proposal_committed"
    PROPOSAL_REJECTED = "proposal_rejected"
    CONTEXT_ASSEMBLED = "context_assembled"
    SESSION_BOOTSTRAPPED = "session_bootstrapped"
    CONFLICT_RECORDED = "conflict_recorded"
