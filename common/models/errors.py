class PRDCPError(Exception):
    """Base exception for expected PRDCP failures."""


class PMPValidationError(PRDCPError):
    pass


class PayloadValidationError(PRDCPError):
    def __init__(
        self,
        message: str,
        *,
        invalid_payload: dict | None = None,
        validation_errors: list[dict] | None = None,
    ) -> None:
        super().__init__(message)
        self.invalid_payload = invalid_payload
        self.validation_errors = validation_errors or []


class AgentExecutionError(PRDCPError):
    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        retry_count: int = 0,
        provider: str | None = None,
        model_id: str | None = None,
        automatic_retry_allowed: bool = True,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.retry_count = retry_count
        self.provider = provider
        self.model_id = model_id
        self.automatic_retry_allowed = automatic_retry_allowed


class RetryableAgentError(AgentExecutionError):
    """A transient provider failure for which one retry may be attempted."""


class ProviderResponseContractError(RetryableAgentError):
    """A billed response that violated the strict JSON response contract.

    Automatic retry is forbidden because the Provider may have completed the
    logical task. A distinct, persisted operator retry is required.
    """

    def __init__(
        self,
        message: str,
        *,
        response_content_sha256: str | None = None,
        response_content_length: int | None = None,
        response_root_type: str | None = None,
        response_invalid_path: str | None = None,
        **kwargs,
    ) -> None:
        kwargs["automatic_retry_allowed"] = False
        super().__init__(message, **kwargs)
        self.response_content_sha256 = response_content_sha256
        self.response_content_length = response_content_length
        self.response_root_type = response_root_type
        self.response_invalid_path = response_invalid_path


class NonRetryableAgentError(AgentExecutionError):
    """A request, contract, or configuration failure that must stop immediately."""


class ProviderCapabilityError(NonRetryableAgentError):
    """The selected model has no endpoint for the required request contract.

    Repeating the same logical task on the same model cannot help. Recovery must
    therefore use a separately authorized task identity and a different model.
    """

    def __init__(self, message: str, **kwargs) -> None:
        kwargs["automatic_retry_allowed"] = False
        super().__init__(message, **kwargs)


class ProviderRequestSchemaError(NonRetryableAgentError):
    """The final provider-bound schema exceeds a known safe request contract.

    This is raised before the provider reservation and HTTP call.  Repeating the
    same request cannot help; code or input specialization must first produce a
    bounded schema.
    """

    def __init__(self, message: str, **kwargs) -> None:
        kwargs["automatic_retry_allowed"] = False
        super().__init__(message, **kwargs)


class WorkflowError(PRDCPError):
    pass
