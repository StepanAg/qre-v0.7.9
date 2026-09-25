class QREError(Exception):
    """Base error for the project."""


class ConfigError(QREError):
    """Invalid or inconsistent configuration."""


class SafetyViolation(QREError):
    """An action was blocked by the safety policy (e.g. order submission)."""


class StorageError(QREError):
    """Persistence layer failure (migrations, integrity)."""
