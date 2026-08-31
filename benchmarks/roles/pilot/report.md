# Local role-model selection pilot

This pilot selects one local arm. **It is not role-recoverability evidence.**

Selected candidate: **Ornith-1.5-35B-A3B**.

Decision: no challenger cleared every frozen switch condition.

## Candidate summary

| Candidate | Exact correct | 3/3 roles | >=2/3 roles | OK | Invalid | Error | Runtime gate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Ornith-1.5-35B-A3B | 15/33 | 5 | 5 | 33 | 0 | 0 | pass |
| Gemma-4-26B-A4B-QAT | 17/33 | 4 | 6 | 33 | 0 | 0 | pass |
| Granite-4.2-3B | 4/33 | 1 | 1 | 33 | 0 | 0 | pass |
| Granite-4.2-8B | 8/33 | 1 | 2 | 33 | 0 | 0 | pass |
| MiniCPM-V-4.6 | 8/33 | 0 | 2 | 33 | 0 | 0 | pass |

## Per-role exact-correct counts

| Role | Ornith-1.5-35B-A3B | Gemma-4-26B-A4B-QAT | Granite-4.2-3B | Granite-4.2-8B | MiniCPM-V-4.6 |
| --- | ---: | ---: | ---: | ---: | ---: |
| unknown | 3/3 | 3/3 | 1/3 | 2/3 | 0/3 |
| claim | 3/3 | 3/3 | 3/3 | 3/3 | 2/3 |
| definition | 0/3 | 0/3 | 0/3 | 1/3 | 1/3 |
| procedure | 3/3 | 3/3 | 0/3 | 1/3 | 2/3 |
| parameter | 0/3 | 0/3 | 0/3 | 0/3 | 1/3 |
| caveat | 3/3 | 3/3 | 0/3 | 0/3 | 0/3 |
| result | 3/3 | 2/3 | 0/3 | 1/3 | 0/3 |
| example | 0/3 | 0/3 | 0/3 | 0/3 | 0/3 |
| decision | 0/3 | 0/3 | 0/3 | 0/3 | 1/3 |
| assumption | 0/3 | 1/3 | 0/3 | 0/3 | 0/3 |
| limitation | 0/3 | 2/3 | 0/3 | 0/3 | 1/3 |

## Switch-condition evaluation

| Challenger | Correct advantage (>=4) | Worse roles (<=2) | Runtime 33/0/0 | Qualifies |
| --- | ---: | ---: | --- | --- |
| Gemma-4-26B-A4B-QAT | 2 | 1 | pass | no |
| Granite-4.2-3B | -11 | 4 | pass | no |
| Granite-4.2-8B | -7 | 4 | pass | no |
| MiniCPM-V-4.6 | -7 | 5 | pass | no |
