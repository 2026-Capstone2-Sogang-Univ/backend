# Experiment Notes

이 문서는 실험 모드에서 사용하는 주요 용어와 실행 기준을 간단히 정리한다. v2 surge/pricing 실험의 상세 실행 방법은 다음 문서를 기준으로 한다.

- `README.experiment.v2-surge.md`

현재 백엔드는 `RideHailingPricingEngine_fixed_v2.py`의 수식과 정책을 반영한 v2 surge/pricing 구조를 사용한다. 내부 계산은 surge와 effective fare 중심이며, 프론트엔드 UI에서 사용하는 incentive 용어는 추가 지불 상한 또는 표시용 금액에 가깝다.

현재 기준:

- surge 상한 기본값은 `4.9`다.
- `incentive_limit`은 수동 승객이 허용하는 추가 지불 상한이다.
- 기사에게 제안하는 금액은 시스템 surge와 승객 cap을 함께 적용한 effective fare 기준이다.
- 실험 모드는 `scripts/run_acceptance_experiment.py`로 실행한다.
- 예측 수요 실험은 `--demand-source predicted`와 `PREDICTION_API_KEY`가 필요하다.
- 승객 생성량은 `PASSENGERS_PER_5MIN` 또는 `--passengers-per-5min`으로 조절한다.

## 용어

| 용어 | 의미 |
| --- | --- |
| `surge` | H3 cell의 supply/demand 불균형과 pricing policy로 계산되는 요금 배수 |
| `effective fare` | system surge와 승객 cap을 적용한 최종 제안 금액 |
| `incentive_limit` | 수동 승객이 허용하는 추가 지불 상한, cent 단위 |
| `target matching rate` | raw surge 구간별 목표 기사 수락률 |
| `PASSENGERS_PER_5MIN` | 5분 bucket당 승객 생성 수 |

새 실험이나 보고서 작성 시 상세 수식과 CLI 사용법은 `README.experiment.v2-surge.md`를 기준으로 한다.
