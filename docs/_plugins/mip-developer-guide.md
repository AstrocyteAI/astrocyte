# MIP developer guide

How to write, test, and debug Memory Intent Protocol routing rules.
MIP routes incoming content to the right bank with the right policy using
declarative rules -- with LLM intent escalation when rules can't decide.

---

## 1. How MIP works

MIP evaluates every `retain()` call through a two-layer pipeline:

1. **Mechanical rules** -- priority-ordered, first-match, zero LLM cost.
2. **Escalation check** -- if no rule matched or confidence is low, check the escalation policy.
3. **LLM intent layer** -- classifies the content and picks a bank, only when mechanical rules can't decide.

Override rules (`override: true`) are checked first and short-circuit the pipeline. Use them for compliance locks that the intent layer must never override.

Config file: `mip.yaml`, referenced from `astrocyte.yaml` via `mip_config_path`. See the Memory Intent Protocol design doc for the full architecture.

---

## 2. File structure

```yaml
version: "1.0"

banks:
  - id: default
    description: General-purpose bank
    access: ["*"]
    compliance: gdpr          # optional
  - id: "student-{student_id}"
    access: [agent:tutor, agent:grader]
    compliance: pdpa
  - id: private-encrypted
    compliance: pdpa

rules:
  - name: example-rule
    priority: 10
    override: false           # optional, default false
    match:
      all:
        - content_type: student_answer
        - metadata.student_id: present
    action:
      bank: "student-{metadata.student_id}"
      tags: ["{metadata.topic}"]
      retain_policy: default  # default | redact_before_store | encrypt | reject

intent_policy:
  escalate_when:
    - matched_rules: 0
    - confidence: { lt: 0.8 }
  model_context: |
    Route content to the correct bank. Banks: {banks}. Tags: {tags}.
  constraints:
    cannot_override: [pii-lockdown]
    must_justify: true
    max_tokens: 200
```

---

## 3. Writing rules

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Unique rule identifier |
| `priority` | int | yes | Lower number = higher priority |
| `match` | block | yes | Conditions to evaluate |
| `action` | block | yes | What to do when matched |
| `override` | bool | no | Compliance lock (checked before normal rules) |

### Match DSL

**Operators:** `present`, `absent`, `eq`, `in`, `gte`, `lte`, `gt`, `lt`

**Fields:** `content_type`, `source`, `pii_detected`, `tags`, `metadata.*`, `signals.*`

**Composition:** `all:` (AND), `any:` (OR), `none:` (NOT)

```yaml
# Shorthand (implicit all)        # Operator form
match:                             match:
  pii_detected: true                 signals.word_count: { gte: 50 }

# Explicit composition             # in operator
match:                             match:
  all:                               all:
    - content_type: student_answer     - content_type: { in: [lesson, quiz] }
    - metadata.student_id: present
  none:
    - source: internal_test
```

### Action DSL

| Field | Type | Description |
|---|---|---|
| `bank` | string | Target bank (supports `{metadata.key}` interpolation) |
| `tags` | list | Tags to attach (supports templates) |
| `retain_policy` | string | `default`, `redact_before_store`, `encrypt`, `reject` |
| `escalate` | string | `"mip"` to force LLM escalation |
| `confidence` | float | Rule confidence score (default 1.0) |

### Practical examples

```yaml
# 1. PII to encrypted bank (override -- always wins)
- name: pii-lockdown
  priority: 1
  override: true
  match: { pii_detected: true }
  action: { bank: private-encrypted, tags: [pii, compliance], retain_policy: redact_before_store }

# 2. Route by content type
- name: conversation-to-dialogue
  priority: 10
  match: { all: [{ content_type: conversation }] }
  action: { bank: dialogue-bank, tags: [conversation] }

# 3. Route by metadata (template interpolation)
- name: student-answer
  priority: 10
  match:
    all:
      - content_type: student_answer
      - metadata.student_id: present
  action:
    bank: "student-{metadata.student_id}"
    tags: ["{metadata.topic}", "{metadata.difficulty}"]

# 4. Route by tag
- name: flagged-content
  priority: 20
  match: { all: [{ tags: { eq: "review-needed" } }] }
  action: { bank: review-queue, tags: [flagged] }

# 5. Reject noise (short content, no source agent)
- name: reject-noise
  priority: 50
  match:
    all:
      - signals.word_count: { lt: 10 }
      - metadata.source_agent: absent
  action: { retain_policy: reject }

# 6. Fallback -- escalate to intent layer
- name: unmatched-fallback
  priority: 999
  match: { all: [] }
  action: { escalate: mip }
```

---

## 4. Intent policy (LLM escalation)

```yaml
intent_policy:
  escalate_when:
    - matched_rules: 0          # no mechanical match
    - confidence: { lt: 0.8 }   # uncertain match
  model_context: |
    Route content to the correct bank. Banks: {banks}. Tags: {tags}.
  constraints:
    cannot_override: [pii-lockdown]
    must_justify: true
    max_tokens: 200
```

**Escalation conditions:** `matched_rules` (number of matches, e.g. `0` = none), `confidence` (top match score below threshold), `conflicting_rules` (set `true` to escalate when multiple rules match). `constraints.cannot_override` protects override rules from LLM override. `must_justify: true` requires LLM reasoning.

---

## 5. Template interpolation

`{field.path}` placeholders in `bank` and `tags` resolve at routing time:

```yaml
bank: "student-{metadata.student_id}"   # -> "student-stu-42"
tags: ["{metadata.topic}"]              # -> ["algebra"]
```

Paths: `{metadata.key}`, `{signals.word_count}`, `{content_type}`, `{source}`. Unresolved placeholders stay literal.

---

## 6. Testing rules locally

### Unit testing with pytest

```python
from astrocyte.mip.schema import RoutingRule, MatchBlock, MatchSpec, ActionSpec
from astrocyte.mip.rule_engine import RuleEngineInput, evaluate_rules

rule = RoutingRule(
    name="pii-to-secure", priority=100,
    match=MatchBlock(all_conditions=[
        MatchSpec(field="pii_detected", operator="eq", value=True)
    ]),
    action=ActionSpec(bank="secure-vault", retain_policy="encrypt"),
)
input_data = RuleEngineInput(
    content="User email is test@example.com",
    metadata={}, tags=[], pii_detected=True,
    signals={"word_count": 6.0},
)

matches = evaluate_rules([rule], input_data)
assert len(matches) == 1
assert matches[0].rule.action.bank == "secure-vault"
```

### Testing the full router

```python
from astrocyte.mip.router import MipRouter
from astrocyte.mip.rule_engine import RuleEngineInput
from astrocyte.mip.schema import *

config = MipConfig(version="1.0",
    banks=[BankDefinition(id="secure-vault", compliance="pdpa")],
    rules=[RoutingRule(
        name="pii-lockdown", priority=1, override=True,
        match=MatchBlock(all_conditions=[
            MatchSpec(field="pii_detected", operator="eq", value=True)]),
        action=ActionSpec(bank="private-encrypted", tags=["pii"],
                          retain_policy="redact_before_store"))])

router = MipRouter(config)
decision = router.route_sync(RuleEngineInput(content="PII here", pii_detected=True))
assert decision.bank_id == "private-encrypted"
assert decision.resolved_by == "mechanical"
```

### Testing from YAML

```python
from astrocyte.mip.loader import load_mip_config
config = load_mip_config("mip.yaml")
router = MipRouter(config)

decision = router.route_sync(RuleEngineInput(
    content="2x+3=7", content_type="student_answer",
    metadata={"student_id": "stu-42", "topic": "algebra"},
))
assert decision.bank_id == "student-stu-42"
```

### Testing escalation

`route_sync()` returns `None` when escalation would fire:

```python
decision = router.route_sync(RuleEngineInput(
    content="Unexpected content", content_type="unknown",
))
assert decision is None  # would escalate to intent layer in production
```

For async escalation with LLM, use `await router.route(input_data)` with a mock `LLMProvider`.

---

## 7. Debugging tips

- **`route_sync()`** for deterministic debugging -- no LLM, pure mechanical rules.
- **`evaluate_rules()` directly** to see all matching rules, not just the winner:
  ```python
  matches = evaluate_rules(rules, input_data)
  for m in matches:
      print(m.rule.name, m.confidence)
  ```
- **Priority:** lower number = higher priority (`1` beats `10`).
- **Log `RuleEngineInput`** to verify what fields are available. Missing metadata is the most common silent failure.
- **Common mistakes:** wrong field path (`studentId` vs `student_id`), `signals.*` values are floats not ints, operator typos (`equals` vs `eq`), forgetting `override: true` on compliance rules.

---

## 8. Loading and env vars

```python
from astrocyte.mip.loader import load_mip_config
config = load_mip_config("mip.yaml")  # YAML parse -> ${ENV_VAR} substitution -> validation
```

Validation errors raise `ConfigError` at load time (duplicate rule names, invalid override+escalate combos). Use `${ENV_VAR}` in any string value: `id: "${TENANT_BANK_ID}"`.

---

## 9. Further reading

- [Memory Intent Protocol](/design/memory-intent-protocol/) — full architecture and rationale
- [Configuration reference](/end-user/configuration-reference/) — `mip_config_path` in `astrocyte.yaml`
- [Architecture](/design/architecture/) — how MIP fits into the Astrocyte retain pipeline
- [Memory API reference](/end-user/memory-api-reference/) — retain/recall/reflect/forget signatures
