# Агент-аналитик с детерминированной проверкой модели (TLA+/TLC)

ИИ-агент получает текстовое описание поведения модуля, строит формальную модель
состояний и переходов (`BehavioralModel`), передаёт её детерминированному Model
Checker (TLA+ / TLC) и объясняет результат на языке исходных требований — с
конкретным контрпримером, гипотезами о причине и уточняющими вопросами.

```
User
 ↓
Analyst Agent (LLM)            analyst_agent/agent.py, prompts.py, llm.py
 ↓
Behavioral Model (JSON DSL)    analyst_agent/schema.py, expressions.py
 ↓
Model Checker Tool             analyst_agent/model_checker.py  (без LLM)
 ├─ Validator                  validator.py
 ├─ TLA+ Compiler              compiler.py
 ├─ TLC Runner (+ SANY)        tlc_runner.py
 └─ Result Parser              result_parser.py
 ↓
Verification Result
 ↓
Analyst Agent → Explanation    explainer.py (опоры) + LLM (формулировка)
```

## Разделение ответственности

| Analyst Agent (LLM) | Model Checker Tool (детерминированный) |
|---|---|
| Понимает смысл требований, выделяет объекты, переменные, события, guards и effects | Валидирует модель |
| Формулирует candidate properties | Компилирует модель в TLA+ и проверяет её SANY |
| Вызывает `check_behavior` | Запускает TLC отдельным процессом с таймаутом и лимитом состояний |
| Объясняет результат на бизнес-языке, формулирует гипотезы и вопросы | Возвращает `PROPERTY_HOLDS` / `PROPERTY_VIOLATED` + контрпример |

LLM **не перебирает состояния**: перебор целиком выполняет TLC. Это зафиксировано и
в системном промпте, и в архитектуре: агент получает факты о достижимости только
из результата инструмента.

## Установка

Нужны Python 3.11+, Java 11+ и `tla2tools.jar`.

```bash
pip install -r requirements.txt          # единственная зависимость — pydantic
./scripts/download_tla.sh                # tla2tools.jar → tools/ (в архиве уже лежит)
```

Путь к jar можно задать через `TLA2TOOLS_JAR`, к Java — через `JAVA_BIN`.

## Быстрый старт

```bash
# 1. Офлайн-демо основного сценария (без LLM API, TLC запускается по-настоящему)
python -m analyst_agent demo

# 2. Проверка готовой модели: JSON-результат в формате постановки
python -m analyst_agent check examples/order_buggy.json
python -m analyst_agent check examples/order_buggy.json --property P1 --explain

# 3. Сгенерированный TLA+ и конфигурация TLC
python -m analyst_agent compile examples/order_buggy.json --property P1

# 4. Агент с реальным LLM (Claude): требования на естественном языке → проверка → объяснение
export ANTHROPIC_API_KEY=...
python -m analyst_agent analyze "Заявка после согласования может быть исполнена. \
  Пользователь может отменить заявку до исполнения." --interactive --save-model model.json
```

Опции движка: `--timeout 60` (с), `--state-limit 1000000` (0 — без лимита),
`--keep-dir out/` (сохранить `.tla`, `.cfg` и лог TLC). Модель LLM меняется через
`--llm-model` или `ANALYST_MODEL`.

Коды выхода `check`: 0 — все свойства выполняются, 1 — есть нарушение, 2 — модель
некорректна, 3 — TIMEOUT / STATE_LIMIT_REACHED / ENGINE_ERROR.

Готовые результаты прогонов лежат в `examples/results/`.

## Основной сценарий

Требование: «Заявка после согласования может быть исполнена. Пользователь может
отменить заявку до исполнения.» Модель `examples/order_buggy.json`: `Execute`
проверяет `approved`, но не статус; `Cancel` не сбрасывает `approved`.

```
P1 «Отменённая заявка не должна быть исполнена» — PROPERTY_VIOLATED
1. Начальное состояние: status=Created, approved=false, cancelled=false.
2. Заявка согласована [Approve]. Изменения: status: Created → Approved; approved: false → true.
3. Пользователь отменяет заявку [Cancel]. Изменения: status: Approved → Cancelled; cancelled: false → true.
   Сохранились: approved=true (признак согласования).
4. Система исполняет заявку [Execute]. Изменения: status: Cancelled → Executed.

Аналитический вывод:
- Execute разрешён условием «approved = true», которое не проверяет cancelled, status.
- Cancel меняет status, cancelled, но не изменяет approved; approved=true, установленное
  Approve, сохраняется и продолжает разрешать Execute.
Уточняющий вопрос: должен ли после отмены сохраняться признак согласования?
```

После того как пользователь подтверждает правило «отмена снимает согласование»,
агент вносит изменение (`examples/order_fixed.json`), перепроверяет — `PROPERTY_HOLDS`.
Полный диалог: `examples/results/demo_dialog.txt`.

Второй пример, `examples/payment_retries.json`, показывает менее очевидную находку:
правило «после *превышения* трёх попыток платёж отклоняется» при лимите в три
попытки делает отклонение **недостижимым** (`reachable` → `PROPERTY_VIOLATED`), а
платёж застревает в `Failed` с `attempts=3` (`deadlock_free` → контрпример).

## DSL модели

```json
{
  "name": "OrderLifecycle",
  "requirements": [{"id": "R1", "text": "Заявка после согласования может быть исполнена."}],
  "variables": [
    {"name": "status", "type": "enum", "values": ["Created", "Approved", "Cancelled", "Executed"], "source": ["R1"]},
    {"name": "approved", "type": "boolean"},
    {"name": "attempts", "type": "integer", "min": 0, "max": 3}
  ],
  "initial": {"status": "Created", "approved": false, "attempts": 0},
  "transitions": [
    {"name": "Approve", "description": "Заявка согласована", "source": ["R1"],
     "guard": {"eq": [{"var": "status"}, "Created"]},
     "effects": [{"set": ["status", "Approved"]}, {"set": ["approved", true]}]}
  ],
  "properties": [
    {"id": "P1", "type": "forbidden_state", "expression": {"and": [
      {"eq": [{"var": "status"}, "Executed"]}, {"eq": [{"var": "cancelled"}, true]}]}},
    {"id": "P3", "type": "reachable", "expression": {"eq": [{"var": "status"}, "Executed"]}},
    {"id": "P4", "type": "deadlock_free", "terminal": {"in": [{"var": "status"}, ["Cancelled", "Executed"]]}}
  ]
}
```

**Выражения:** литерал (`"Created"`, `true`, `3`) · `{"var": name}` · `eq, neq, lt, le, gt, ge, add, sub: [a, b]` ·
`and, or: [a, ...]` · `{"not": a}` · `{"implies": [a, b]}` · `{"in": [a, [литералы]]}`.
Строка — всегда литерал; переменная — только через `{"var": ...}`.

**Семантика переходов:** переменные, не упомянутые в `effects`, не меняются
(`UNCHANGED`); правые части вычисляются в состоянии до перехода (параллельное
присваивание); если вычисляемое значение integer-переменной выходит за `[min, max]`,
переход недоступен (домены конечны).

**Свойства:**

| type | Смысл | Компиляция в TLC | Результат |
|---|---|---|---|
| `invariant` | истинно во всех достижимых состояниях | `INVARIANT expr` | нарушение → контрпример |
| `forbidden_state` | состояние никогда не достижимо | `INVARIANT ~(expr)` | нарушение → контрпример |
| `reachable` | есть путь к состоянию | `INVARIANT ~(expr)` | найдено → `PROPERTY_HOLDS` + `witness`; не найдено → `PROPERTY_VIOLATED` |
| `deadlock_free` | нет нефинальных состояний без переходов | проверка deadlock TLC; `terminal` получает шаг на месте | deadlock → контрпример |

**Ссылки на требования:** `requirements` + поле `source` у переменных, переходов и
свойств. В результате — `related_requirements`: требования свойства и всех
переходов контрпримера.

## Model Checker Tool

`check_behavior(model, property_ids=None, config=None) -> CheckReport`, по одному
`VerificationResult` на свойство (каждое свойство — отдельный запуск TLC, чтобы
результат однозначно относился к `property_id`).

**Этап 1 — валидация** (`validator.py`): уникальность имён переменных, переходов и
id свойств; корректность типов и их атрибутов; наличие начальных значений для всех
переменных и отсутствие лишних (ловит и опечатку `"attempts "` с пробелом из
постановки); попадание начальных значений в домены; ссылки только на существующие
переменные; проверка типов в guards, effects и properties (включая значения enum —
`"Canceled"` вместо `"Cancelled"` будет ошибкой); конечность доменов (integer обязан
иметь `min` и `max`); корректность свойств; имена — допустимые идентификаторы TLA+,
не совпадающие с зарезервированными. Предупреждения: переход без эффектов,
переменная, которую никто не меняет, ссылка на неизвестное требование, слишком
большое пространство состояний.

**Этап 2 — компиляция** (`compiler.py`) в `.tla` + `.cfg` и разбор SANY (ошибка
SANY считается ошибкой компилятора → `ENGINE_ERROR`). Служебный инвариант
`MC_TypeOK` дополнительно страхует корректность компиляции.

**Этап 3–4 — запуск TLC** (`tlc_runner.py`): отдельный subprocess в своей группе
процессов; по таймауту вся группа завершается `SIGKILL`. Перебор состояний делает TLC.

**Этап 5–6 — разбор** (`result_parser.py`): статус, статистика (`states_generated`,
`distinct_states`, `depth`) и трасса от начального состояния до первого нарушения с
восстановленными именами переходов (каждый переход компилируется в отдельный
оператор TLA+, и TLC печатает его имя в заголовке шага). К каждому шагу добавлено
поле `changes` — что изменилось.

**Статусы:** `PROPERTY_HOLDS`, `PROPERTY_VIOLATED`, `MODEL_INVALID`, `TIMEOUT`,
`STATE_LIMIT_REACHED`, `ENGINE_ERROR`. `TIMEOUT` и `STATE_LIMIT_REACHED` никогда не
превращаются в `PROPERTY_HOLDS` (покрыто тестами). Если нарушение найдено до
исчерпания лимита, это честный `PROPERTY_VIOLATED`. `PROPERTY_HOLDS` всегда
сопровождается оговоркой: свойство не нарушено в конечной модели, это не
доказательство корректности production-системы.

Лимит состояний реализован ограничением `CONSTRAINT TLCGet("distinct") <= N`:
состояния сверх лимита не раскрываются, и если TLC насчитал больше N различных
состояний, пространство исследовано не полностью → `STATE_LIMIT_REACHED`.

## Analyst Agent

`AnalystAgent(llm).ask(text)` — цикл с инструментами `check_behavior` и
`validate_model`. Системный промпт (`prompts.py`) задаёт порядок работы (объект →
переменные → события → переходы → свойства → `check_behavior` → интерпретация),
DSL, правила чтения статусов и формат объяснения: итог → сценарий шагами на
бизнес-языке → аналитический вывод со ссылками на требования → гипотезы → вопросы.

К каждому результату инструмент прикладывает `explanation_aids`, которые
детерминированно строит `explainer.py`:
- `business_steps` — шаги контрпримера словами описаний переходов, с изменениями и
  «сохранившимися» значениями, важными для нарушения;
- `diagnostic_hints` — какое условие разрешило последний переход и чего оно не
  проверяет; какой промежуточный переход не сбросил признак; какие условия
  невыполнимы при данных доменах (`attempts > 3` при `0..3`);
- `hypotheses` и `questions`.

LLM пересказывает эти факты языком требований — объяснение опирается на
результат проверки, а не на догадки модели.

**Агент не исправляет модель автоматически.** Помимо инструкции в промпте, это
обеспечено технически: если в текущем ходе найден контрпример, повторный вызов
`check_behavior` с изменённой логикой модели блокируется (`POLICY_NO_AUTOFIX`).
Разрешено проверять ту же модель по другим свойствам и исправлять
`MODEL_INVALID` (это ошибка записи модели, а не логики). Исправление логики
возможно только в следующем ходе, после подтверждения пользователем.

LLM-клиент (`llm.py`) обращается к Claude Messages API напрямую через `urllib` (без
SDK). Чтобы подключить другого провайдера, реализуйте метод
`complete(system, messages, tools) -> LLMResponse`. `ScriptedLLM` — детерминированная
замена для тестов и офлайн-демо.

## Тесты

```bash
python -m unittest discover -s tests -t .     # или: pytest
```

| Набор | Файл | Что проверяет |
|---|---|---|
| Unit: JSON validation | `test_validator.py` | все правила этапа 1 → ожидаемые коды ошибок |
| Unit: Expression → TLA | `test_expressions.py` | компиляция операторов, разбор, проверка типов |
| Unit: Model → TLA | `test_compiler.py` | Init, переходы, UNCHANGED, Next, свойства всех типов, cfg |
| Unit: TLC trace → result | `test_result_parser.py` | трассы, deadlock, нарушение в начальном состоянии, статистика |
| Integration | `test_integration_tlc.py` | Acceptance №1 и №2, reachable / deadlock, TIMEOUT, STATE_LIMIT_REACHED, ENGINE_ERROR |
| Agent | `test_agent.py` | NL → модель → TLC → объяснение; политика no-autofix; механика цикла |

Агентные тесты используют `ScriptedLLM` (ответы LLM записаны), а TLC запускается
по-настоящему. Живой тест с Claude: `ANTHROPIC_API_KEY=... RUN_LIVE_LLM=1 python -m unittest tests.test_agent`.

## Ограничения и развитие

- DSL покрывает конечные модели с enum, boolean и integer; нет коллекций,
  нескольких экземпляров сущностей и параметризованных действий.
- Свойства — инварианты, запрещённые состояния, достижимость и deadlock;
  темпоральные свойства (liveness, «рано или поздно») — следующий шаг (`PROPERTY` в TLC).
- `PROPERTY_HOLDS` относится к конечной модели с заданными доменами.
