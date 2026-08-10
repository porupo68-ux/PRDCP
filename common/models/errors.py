class PRDCPError(Exception):
    """Base exception for expected PRDCP failures."""


class PMPValidationError(PRDCPError):
    pass


class PayloadValidationError(PRDCPError):
    pass


class AgentExecutionError(PRDCPError):
    pass


class WorkflowError(PRDCPError):
    pass

