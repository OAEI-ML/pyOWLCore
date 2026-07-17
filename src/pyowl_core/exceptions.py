"""Stable public exception and warning taxonomy for pyowl-core."""

from __future__ import annotations

from typing import ClassVar

from .diagnostics import Diagnostic, Severity, validate_diagnostic_code


class PyOWLCoreError(Exception):
    DEFAULT_CODE: ClassVar[str] = "PYOWL_CORE_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        diagnostic: Diagnostic | None = None,
    ) -> None:
        if not isinstance(message, str) or not message:
            raise ValueError("exception message must be a nonempty string")
        selected_code = diagnostic.code if code is None and diagnostic is not None else code
        self.code = validate_diagnostic_code(selected_code or self.DEFAULT_CODE)
        if diagnostic is not None and diagnostic.code != self.code:
            raise ValueError("exception code and diagnostic code must match")
        self.diagnostic = diagnostic
        super().__init__(message)

    def as_diagnostic(self, *, severity: Severity = Severity.ERROR) -> Diagnostic:
        if self.diagnostic is not None:
            return self.diagnostic
        return Diagnostic(code=self.code, severity=severity, message=str(self))


class ModelError(PyOWLCoreError):
    DEFAULT_CODE = "MODEL_ERROR"


class InvalidIRIError(ModelError):
    DEFAULT_CODE = "INVALID_IRI"


class InvalidLiteralError(ModelError):
    DEFAULT_CODE = "INVALID_LITERAL"


class StructuralConstraintError(ModelError):
    DEFAULT_CODE = "STRUCTURAL_CONSTRAINT"


class ParseError(PyOWLCoreError):
    DEFAULT_CODE = "PARSE_ERROR"


class FormatDetectionError(ParseError):
    DEFAULT_CODE = "FORMAT_DETECTION"


class OntologySyntaxError(ParseError):
    DEFAULT_CODE = "ONTOLOGY_SYNTAX"


class UnsupportedSyntaxError(ParseError):
    DEFAULT_CODE = "UNSUPPORTED_SYNTAX"


class ImportResolutionError(PyOWLCoreError):
    DEFAULT_CODE = "IMPORT_RESOLUTION"


class UnresolvedImportError(ImportResolutionError):
    DEFAULT_CODE = "UNRESOLVED_IMPORT"


class ImportCycleError(ImportResolutionError):
    """A resolver alias/redirect/policy cycle, never a legal OWL import cycle."""

    DEFAULT_CODE = "IMPORT_RESOLUTION_CYCLE"


class DocumentIdentityConflictError(ImportResolutionError):
    DEFAULT_CODE = "DOCUMENT_IDENTITY_CONFLICT"


class IntegrityError(ImportResolutionError):
    DEFAULT_CODE = "INTEGRITY_ERROR"


class AccessDeniedError(ImportResolutionError):
    DEFAULT_CODE = "ACCESS_DENIED"


class ProfileError(PyOWLCoreError):
    DEFAULT_CODE = "PROFILE_ERROR"


class ResourceLimitError(PyOWLCoreError):
    DEFAULT_CODE = "RESOURCE_LIMIT"

    def __init__(
        self,
        message: str,
        *,
        limit: str | None = None,
        observed: int | float | None = None,
        allowed: int | float | None = None,
        code: str | None = None,
        diagnostic: Diagnostic | None = None,
    ) -> None:
        self.limit = limit
        self.observed = observed
        self.allowed = allowed
        super().__init__(message, code=code, diagnostic=diagnostic)


class OperationCancelledError(PyOWLCoreError):
    DEFAULT_CODE = "OPERATION_CANCELLED"

    def __init__(
        self,
        message: str = "operation cancelled",
        *,
        reason: str | None = None,
        code: str | None = None,
        diagnostic: Diagnostic | None = None,
    ) -> None:
        self.reason = reason
        super().__init__(message, code=code, diagnostic=diagnostic)


class ReentrancyError(PyOWLCoreError):
    DEFAULT_CODE = "REENTRANCY"


class BackendError(PyOWLCoreError):
    DEFAULT_CODE = "BACKEND_ERROR"


class BackendUnavailableError(BackendError):
    DEFAULT_CODE = "BACKEND_UNAVAILABLE"


class BackendProtocolError(BackendError):
    DEFAULT_CODE = "BACKEND_PROTOCOL"


class WireError(PyOWLCoreError):
    DEFAULT_CODE = "WIRE_ERROR"


class WireVersionError(WireError):
    DEFAULT_CODE = "WIRE_VERSION"


class WireCorruptionError(WireError):
    DEFAULT_CODE = "WIRE_CORRUPTION"


class WireLimitError(WireError):
    DEFAULT_CODE = "WIRE_LIMIT"


class DeltaError(PyOWLCoreError):
    DEFAULT_CODE = "DELTA_ERROR"


class DeltaBaseMismatchError(DeltaError):
    DEFAULT_CODE = "DELTA_BASE_MISMATCH"


class OptionConflictError(PyOWLCoreError):
    DEFAULT_CODE = "OPTION_CONFLICT"


class SnapshotLifecycleError(PyOWLCoreError):
    DEFAULT_CODE = "SNAPSHOT_LIFECYCLE"


class ClosedSnapshotError(SnapshotLifecycleError):
    DEFAULT_CODE = "CLOSED_SNAPSHOT"


class SnapshotInUseError(SnapshotLifecycleError):
    DEFAULT_CODE = "SNAPSHOT_IN_USE"


class AdapterError(PyOWLCoreError):
    DEFAULT_CODE = "ADAPTER_ERROR"


class AdapterCompatibilityError(AdapterError):
    DEFAULT_CODE = "ADAPTER_COMPATIBILITY"


class PyOWLCoreWarning(Warning):
    DEFAULT_CODE: ClassVar[str] = "PYOWL_CORE_WARNING"


class NativeBackendUnavailableWarning(PyOWLCoreWarning, RuntimeWarning):
    DEFAULT_CODE = "NATIVE_BACKEND_UNAVAILABLE"


class UnresolvedImportWarning(PyOWLCoreWarning, UserWarning):
    DEFAULT_CODE = "UNRESOLVED_IMPORT"


class OverlayPerformanceWarning(PyOWLCoreWarning, RuntimeWarning):
    DEFAULT_CODE = "OVERLAY_PERFORMANCE"


class FormatGuessWarning(PyOWLCoreWarning, UserWarning):
    DEFAULT_CODE = "FORMAT_GUESS"


class LossyRenderWarning(PyOWLCoreWarning, UserWarning):
    DEFAULT_CODE = "LOSSY_RENDER"


class DeprecatedAPIWarning(DeprecationWarning):
    DEFAULT_CODE: ClassVar[str] = "DEPRECATED_API"


__all__ = [
    "AccessDeniedError",
    "AdapterCompatibilityError",
    "AdapterError",
    "BackendError",
    "BackendProtocolError",
    "BackendUnavailableError",
    "ClosedSnapshotError",
    "DeltaBaseMismatchError",
    "DeltaError",
    "DeprecatedAPIWarning",
    "DocumentIdentityConflictError",
    "FormatDetectionError",
    "FormatGuessWarning",
    "ImportCycleError",
    "ImportResolutionError",
    "IntegrityError",
    "InvalidIRIError",
    "InvalidLiteralError",
    "LossyRenderWarning",
    "ModelError",
    "NativeBackendUnavailableWarning",
    "OntologySyntaxError",
    "OperationCancelledError",
    "OptionConflictError",
    "OverlayPerformanceWarning",
    "ParseError",
    "ProfileError",
    "PyOWLCoreError",
    "PyOWLCoreWarning",
    "ReentrancyError",
    "ResourceLimitError",
    "SnapshotInUseError",
    "SnapshotLifecycleError",
    "StructuralConstraintError",
    "UnresolvedImportError",
    "UnresolvedImportWarning",
    "UnsupportedSyntaxError",
    "WireCorruptionError",
    "WireError",
    "WireLimitError",
    "WireVersionError",
]
