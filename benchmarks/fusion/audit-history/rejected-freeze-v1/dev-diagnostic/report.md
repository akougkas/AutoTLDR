# Stage 4 model-free fusion report (dev)

This development report is diagnostic only; scored gates are not applied.

| Signal | Support | Groups | TP | FP | FN | Precision | Recall | F1 | Disposition |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `literal` | 6 | 2 | 6 | 0 | 0 | 1.000 | 1.000 | 1.000 | diagnostic-only |
| `identifier` | 54 | 2 | 48 | 2 | 6 | 0.960 | 0.889 | 0.923 | diagnostic-only |
| `structural` | 2 | 2 | 2 | 0 | 0 | 1.000 | 1.000 | 1.000 | diagnostic-only |
| `contradiction` | 2 | 2 | 2 | 0 | 0 | 1.000 | 1.000 | 1.000 | diagnostic-only |
| `orphan` | 2 | 2 | 2 | 0 | 0 | 1.000 | 1.000 | 1.000 | diagnostic-only |
| `unresolved` | 2 | 2 | 2 | 0 | 0 | 1.000 | 1.000 | 1.000 | diagnostic-only |

## Signal details

### literal

Gate passed: **not evaluated on dev**. Precision Wilson 95%: `[0.609666, 1.0]`; recall Wilson 95%: `[0.609666, 1.0]`.

Error inventory: 0 unique FP key(s), 0 duplicate FP occurrence(s), and 0 FN key(s). The stable serialized keys are in `report.json`.

Hard-negative false positives: `{}`.

Subtype attribution: `{"ambiguous_predictions": 0, "assignment_counts": {"local-path": 6}, "basis": ["ref_kind+resolution"], "complete": true, "gold_complete": true, "prediction_complete": true, "unclassified_prediction_keys": []}`.

| Subtype | Support | Groups | TP | FP | FN | Precision | Recall | F1 | Eligible |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `local-path` | 6 | 2 | 6 | 0 | 0 | 1.000 | 1.000 | 1.000 | yes |

### identifier

Gate passed: **not evaluated on dev**. Precision Wilson 95%: `[0.865399, 0.988961]`; recall Wilson 95%: `[0.778053, 0.94807]`.

Error inventory: 2 unique FP key(s), 0 duplicate FP occurrence(s), and 6 FN key(s). The stable serialized keys are in `report.json`.

Hard-negative false positives: `{}`.

Subtype attribution: `{"ambiguous_predictions": 0, "assignment_counts": {"explicit-prose-native": 29, "native-native": 18, "prose-prose": 3}, "basis": ["left_native+right_native"], "complete": false, "gold_complete": false, "prediction_complete": true, "unclassified_prediction_keys": []}`.

| Subtype | Support | Groups | TP | FP | FN | Precision | Recall | F1 | Eligible |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `explicit-prose-native` | 0 | 0 | 0 | 29 | 0 | 0.000 | — | — | no |
| `native-native` | 54 | 2 | 18 | 0 | 36 | 1.000 | 0.333 | 0.500 | no |
| `prose-prose` | 0 | 0 | 0 | 3 | 0 | 0.000 | — | — | no |

### structural

Gate passed: **not evaluated on dev**. Precision Wilson 95%: `[0.34238, 1.0]`; recall Wilson 95%: `[0.34238, 1.0]`.

Error inventory: 0 unique FP key(s), 0 duplicate FP occurrence(s), and 0 FN key(s). The stable serialized keys are in `report.json`.

Hard-negative false positives: `{}`.

Subtype attribution: `{"ambiguous_predictions": 0, "assignment_counts": {"record-schema-record-schema": 1, "table-record-schema": 1}, "basis": ["source_kind+container_family"], "complete": true, "gold_complete": true, "prediction_complete": true, "unclassified_prediction_keys": []}`.

| Subtype | Support | Groups | TP | FP | FN | Precision | Recall | F1 | Eligible |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `record-schema-record-schema` | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 1.000 | yes |
| `table-record-schema` | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 1.000 | yes |

### contradiction

Gate passed: **not evaluated on dev**. Precision Wilson 95%: `[0.34238, 1.0]`; recall Wilson 95%: `[0.34238, 1.0]`.

Error inventory: 0 unique FP key(s), 0 duplicate FP occurrence(s), and 0 FN key(s). The stable serialized keys are in `report.json`.

Hard-negative false positives: `{}`.

Subtype attribution: `{"ambiguous_predictions": 0, "assignment_counts": {"constant-constant": 2}, "basis": ["left_basis+right_basis"], "complete": true, "gold_complete": true, "prediction_complete": true, "unclassified_prediction_keys": []}`.

| Subtype | Support | Groups | TP | FP | FN | Precision | Recall | F1 | Eligible |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `constant-constant` | 2 | 2 | 2 | 0 | 0 | 1.000 | 1.000 | 1.000 | yes |

### orphan

Gate passed: **not evaluated on dev**. Precision Wilson 95%: `[0.34238, 1.0]`; recall Wilson 95%: `[0.34238, 1.0]`.

Error inventory: 0 unique FP key(s), 0 duplicate FP occurrence(s), and 0 FN key(s). The stable serialized keys are in `report.json`.

Hard-negative false positives: `{}`.

Subtype attribution: `{"ambiguous_predictions": 0, "assignment_counts": {"no-accepted-relation": 2}, "basis": ["absence-of-accepted-cross-source-relation"], "complete": true, "gold_complete": true, "prediction_complete": true, "unclassified_prediction_keys": []}`.

| Subtype | Support | Groups | TP | FP | FN | Precision | Recall | F1 | Eligible |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `no-accepted-relation` | 2 | 2 | 2 | 0 | 0 | 1.000 | 1.000 | 1.000 | yes |

### unresolved

Gate passed: **not evaluated on dev**. Precision Wilson 95%: `[0.34238, 1.0]`; recall Wilson 95%: `[0.34238, 1.0]`.

Error inventory: 0 unique FP key(s), 0 duplicate FP occurrence(s), and 0 FN key(s). The stable serialized keys are in `report.json`.

Hard-negative false positives: `{}`.

Subtype attribution: `{"ambiguous_predictions": 0, "assignment_counts": {"local-path": 2}, "basis": ["ref_kind+reason"], "complete": true, "gold_complete": true, "prediction_complete": true, "unclassified_prediction_keys": []}`.

| Subtype | Support | Groups | TP | FP | FN | Precision | Recall | F1 | Eligible |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `local-path` | 2 | 2 | 2 | 0 | 0 | 1.000 | 1.000 | 1.000 | yes |

## Limitations

This is a synthetic diagnostic corpus over real production extractors. It is not an estimate of production prevalence. Labels were frozen before predictions, but the semantic annotations have not received a human domain-expert audit.
