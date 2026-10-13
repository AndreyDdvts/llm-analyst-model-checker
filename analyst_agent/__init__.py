"""Агент-аналитик: требования → формальная модель → TLA+/TLC → объяснение."""

from .agent import AnalystAgent
from .model_checker import CheckerConfig, CheckReport, ModelChecker, check_behavior
from .schema import BehavioralModel, ResultStatus, VerificationResult

__all__ = [
    "AnalystAgent",
    "BehavioralModel",
    "CheckReport",
    "CheckerConfig",
    "ModelChecker",
    "ResultStatus",
    "VerificationResult",
    "check_behavior",
]
__version__ = "0.1.0"
