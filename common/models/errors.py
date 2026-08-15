class PRDCPError(Exception):
    """Base exception for expected PRDCP failures."""


class PMPValidationError(PRDCPError):
    pass


class PayloadValidationError(PRDCPError):
    pass


class AgentExecutionError(PRDCPError):
    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        retry_count: int = 0,
        provider: str | None = None,
        model_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.retry_count = retry_count
        self.provider = provider
        self.model_id = model_id


class RetryableAgentError(AgentExecutionError):
    """A transient provider failure for which one retry may be attempted."""


class NonRetryableAgentError(AgentExecutionError):
    """A request, contract, or configuration failure that must stop immediately."""


class WorkflowError(PRDCPError):
    pass
