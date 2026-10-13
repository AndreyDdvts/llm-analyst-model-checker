"""Pydantic-схемы входных и выходных данных.

BehavioralModel  — формальная модель поведения (вход Model Checker Tool).
VerificationResult — результат проверки одного свойства (выход Model Checker Tool).

Схемы описывают только *структуру*. Семантические проверки (ссылки на
существующие переменные, типы выражений, попадание в домены и т.п.)
выполняет validator.py.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

# Выражение DSL хранится как «сырой» JSON (dict / литерал) и разбирается
# в expressions.py. Так сообщения об ошибках получаются понятнее, чем при
# попытке описать рекурсивный DSL через Union-модели.
Expression = Any


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Входная модель
# --------------------------------------------------------------------------


class Requirement(_Strict):
    """Исходное требование (фрагмент текста пользователя)."""

    id: str
    text: str


class VarType(str, Enum):
    enum = "enum"
    boolean = "boolean"
    integer = "integer"


class Variable(_Strict):
    name: str
    type: VarType
    values: Optional[list[str]] = None  # для enum
    min: Optional[int] = None  # для integer
    max: Optional[int] = None  # для integer
    description: Optional[str] = None
    source: list[str] = Field(default_factory=list)  # id требований


class Transition(_Strict):
    """Одно допустимое изменение состояния: name + guard + effects.

    effects: [{"set": ["var", <expression>]}, ...]
    Все правые части вычисляются в состоянии *до* перехода (параллельное
    присваивание, как в TLA+). Переменные, не упомянутые в effects,
    не меняются (UNCHANGED).
    """

    name: str
    guard: Expression = True
    effects: list[dict[str, Any]] = Field(default_factory=list)
    description: Optional[str] = None
    source: list[str] = Field(default_factory=list)


class PropertyType(str, Enum):
    invariant = "invariant"
    forbidden_state = "forbidden_state"
    reachable = "reachable"
    deadlock_free = "deadlock_free"


class Property(_Strict):
    id: str
    type: PropertyType
    expression: Optional[Expression] = None
    # Только для deadlock_free: состояния, в которых остановка допустима
    # (финальные состояния бизнес-процесса). Если не задано — любое
    # состояние без исходящих переходов считается deadlock.
    terminal: Optional[Expression] = None
    description: Optional[str] = None
    source: list[str] = Field(default_factory=list)


class BehavioralModel(_Strict):
    name: str = "Model"
    description: Optional[str] = None
    requirements: list[Requirement] = Field(default_factory=list)
    variables: list[Variable]
    initial: dict[str, Any]
    transitions: list[Transition]
    properties: list[Property] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Результат проверки
# --------------------------------------------------------------------------


class ResultStatus(str, Enum):
    PROPERTY_HOLDS = "PROPERTY_HOLDS"
    PROPERTY_VIOLATED = "PROPERTY_VIOLATED"
    MODEL_INVALID = "MODEL_INVALID"
    TIMEOUT = "TIMEOUT"
    STATE_LIMIT_REACHED = "STATE_LIMIT_REACHED"
    ENGINE_ERROR = "ENGINE_ERROR"


class ValidationIssue(BaseModel):
    code: str
    path: str
    message: str
    severity: Literal["error", "warning"] = "error"


class Statistics(BaseModel):
    states_generated: Optional[int] = None
    distinct_states: Optional[int] = None
    depth: Optional[int] = None


class TraceStep(BaseModel):
    number: int
    transition: Optional[str] = None  # None для начального состояния
    state: dict[str, Union[bool, int, str]]
    changes: dict[str, list[Union[bool, int, str]]] = Field(
        default_factory=dict,
        description="var -> [старое значение, новое значение]",
    )


class Trace(BaseModel):
    steps: list[TraceStep]

    def transitions(self) -> list[str]:
        return [s.transition for s in self.steps if s.transition]


class VerificationResult(BaseModel):
    status: ResultStatus
    property_id: Optional[str] = None
    property_type: Optional[PropertyType] = None
    engine: str = "TLC"
    statistics: Optional[Statistics] = None
    counterexample: Optional[Trace] = None
    # для reachable: путь-свидетель, доказывающий достижимость
    witness: Optional[Trace] = None
    related_requirements: list[Requirement] = Field(default_factory=list)
    issues: list[ValidationIssue] = Field(default_factory=list)
    message: Optional[str] = None
    duration_sec: Optional[float] = None

    def to_public_dict(self) -> dict[str, Any]:
        """JSON в формате постановки (без пустых полей)."""
        return self.model_dump(mode="json", exclude_none=True, exclude_defaults=False)
