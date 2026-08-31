# Stage 4 model-free fusion report (scored)

The gate is per signal. Aggregate accuracy is deliberately not used.

| Signal | Support | Groups | TP | FP | FN | Precision | Recall | F1 | Disposition |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `literal` | 23 | 6 | 22 | 0 | 1 | 1.000 | 0.957 | 0.978 | ship-complete |
| `identifier` | 171 | 6 | 142 | 25 | 29 | 0.850 | 0.830 | 0.840 | ship-preregistered-subtype |
| `structural` | 10 | 6 | 8 | 0 | 2 | 1.000 | 0.800 | 0.889 | ship-complete |
| `contradiction` | 12 | 6 | 8 | 0 | 4 | 1.000 | 0.667 | 0.800 | disable |
| `orphan` | 7 | 6 | 4 | 0 | 3 | 1.000 | 0.571 | 0.727 | disable |
| `unresolved` | 9 | 6 | 9 | 1 | 0 | 0.900 | 1.000 | 0.947 | ship-preregistered-subtype |

## Signal details

### literal

Gate passed: **yes**. Precision Wilson 95%: `[0.851345, 1.0]`; recall Wilson 95%: `[0.790088, 0.992283]`.

Error inventory: 0 unique FP key(s), 0 duplicate FP occurrence(s), and 1 FN key(s). The stable serialized keys are in `report.json`.

Hard-negative false positives: `{}`.

Subtype attribution: `{"ambiguous_predictions": 0, "assignment_counts": {"local-path": 22}, "basis": ["ref_kind+resolution"], "complete": true, "gold_complete": true, "prediction_complete": true, "unclassified_prediction_keys": []}`.

| Subtype | Support | Groups | TP | FP | FN | Precision | Recall | F1 | Eligible |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `label-key` | 1 | 1 | 0 | 0 | 1 | — | 0.000 | — | yes |
| `local-path` | 22 | 6 | 22 | 0 | 0 | 1.000 | 1.000 | 1.000 | yes |

### identifier

Gate passed: **no**. Precision Wilson 95%: `[0.788347, 0.896499]`; recall Wilson 95%: `[0.767053, 0.879247]`.

Error inventory: 25 unique FP key(s), 0 duplicate FP occurrence(s), and 29 FN key(s). The stable serialized keys are in `report.json`.

Hard-negative false positives: `{}`.

Subtype attribution: `{"ambiguous_predictions": 0, "assignment_counts": {"explicit-prose-native": 91, "native-native": 60, "prose-prose": 16}, "basis": ["left_native+right_native"], "complete": true, "gold_complete": true, "prediction_complete": true, "unclassified_prediction_keys": []}`.

| Subtype | Support | Groups | TP | FP | FN | Precision | Recall | F1 | Eligible |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `explicit-prose-native` | 96 | 6 | 79 | 12 | 17 | 0.868 | 0.823 | 0.845 | yes |
| `native-native` | 63 | 6 | 54 | 6 | 9 | 0.900 | 0.857 | 0.878 | yes |
| `prose-prose` | 12 | 2 | 9 | 7 | 3 | 0.562 | 0.750 | 0.643 | yes |

### structural

Gate passed: **yes**. Precision Wilson 95%: `[0.675592, 1.0]`; recall Wilson 95%: `[0.490162, 0.943318]`.

Error inventory: 0 unique FP key(s), 0 duplicate FP occurrence(s), and 2 FN key(s). The stable serialized keys are in `report.json`.

Hard-negative false positives: `{}`.

Subtype attribution: `{"ambiguous_predictions": 0, "assignment_counts": {"record-schema-record-schema": 3, "table-record-schema": 5}, "basis": ["source_kind+container_family"], "complete": true, "gold_complete": true, "prediction_complete": true, "unclassified_prediction_keys": []}`.

| Subtype | Support | Groups | TP | FP | FN | Precision | Recall | F1 | Eligible |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `record-schema-record-schema` | 3 | 3 | 3 | 0 | 0 | 1.000 | 1.000 | 1.000 | yes |
| `table-record-schema` | 7 | 5 | 5 | 0 | 2 | 1.000 | 0.714 | 0.833 | yes |

### contradiction

Gate passed: **no**. Precision Wilson 95%: `[0.675592, 1.0]`; recall Wilson 95%: `[0.390622, 0.86188]`.

Error inventory: 0 unique FP key(s), 0 duplicate FP occurrence(s), and 4 FN key(s). The stable serialized keys are in `report.json`.

Hard-negative false positives: `{}`.

Subtype attribution: `{"ambiguous_predictions": 0, "assignment_counts": {"constant-constant": 8}, "basis": ["left_basis+right_basis"], "complete": true, "gold_complete": true, "prediction_complete": true, "unclassified_prediction_keys": []}`.

| Subtype | Support | Groups | TP | FP | FN | Precision | Recall | F1 | Eligible |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `constant-constant` | 12 | 6 | 8 | 0 | 4 | 1.000 | 0.667 | 0.800 | yes |

### orphan

Gate passed: **no**. Precision Wilson 95%: `[0.510109, 1.0]`; recall Wilson 95%: `[0.250458, 0.84178]`.

Error inventory: 0 unique FP key(s), 0 duplicate FP occurrence(s), and 3 FN key(s). The stable serialized keys are in `report.json`.

Hard-negative false positives: `{}`.

Subtype attribution: `{"ambiguous_predictions": 0, "assignment_counts": {"no-accepted-relation": 4}, "basis": ["absence-of-accepted-cross-source-relation"], "complete": true, "gold_complete": true, "prediction_complete": true, "unclassified_prediction_keys": []}`.

| Subtype | Support | Groups | TP | FP | FN | Precision | Recall | F1 | Eligible |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `no-accepted-relation` | 7 | 6 | 4 | 0 | 3 | 1.000 | 0.571 | 0.727 | yes |

### unresolved

Gate passed: **no**. Precision Wilson 95%: `[0.59585, 0.982124]`; recall Wilson 95%: `[0.700855, 1.0]`.

Error inventory: 1 unique FP key(s), 0 duplicate FP occurrence(s), and 0 FN key(s). The stable serialized keys are in `report.json`.

Hard-negative false positives: `{}`.

Subtype attribution: `{"ambiguous_predictions": 0, "assignment_counts": {"ambiguous-local-path": 1, "citation-key": 1, "label-key": 2, "local-path": 6}, "basis": ["ref_kind+reason"], "complete": true, "gold_complete": true, "prediction_complete": true, "unclassified_prediction_keys": []}`.

| Subtype | Support | Groups | TP | FP | FN | Precision | Recall | F1 | Eligible |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `ambiguous-local-path` | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 1.000 | yes |
| `citation-key` | 1 | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 1.000 | yes |
| `label-key` | 1 | 1 | 1 | 1 | 0 | 0.500 | 1.000 | 0.667 | yes |
| `local-path` | 6 | 5 | 6 | 0 | 0 | 1.000 | 1.000 | 1.000 | yes |

## Limitations

This is a synthetic diagnostic corpus over real production extractors. It is not an estimate of production prevalence. Labels were frozen before predictions, but the semantic annotations have not received a human domain-expert audit.
