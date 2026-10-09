class OCRAgentError(Exception):
    """Base error for expected OCR-Agent failures."""


class SchemaError(OCRAgentError):
    """Input or model output does not satisfy the contract."""


class OperationError(OCRAgentError):
    """A requested edit is unsafe or invalid."""


class ProviderError(OCRAgentError):
    """Remote model request failed."""


class ProviderConfigError(ProviderError):
    """Remote model configuration is missing or invalid."""


class ProviderTransientError(ProviderError):
    """Remote model request may succeed when retried."""


class ProviderPermanentError(ProviderError):
    """Remote model request should not be retried unchanged."""
