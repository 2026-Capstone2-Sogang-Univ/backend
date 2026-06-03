from app.policy_comparison import compare_policy_ab


def test_compare_policy_ab_detects_matching_improvement():
    actual = {"matching_success_rate": 0.8, "passengers_never_offered_rate": 0.2}
    predicted = {"matching_success_rate": 0.9, "passengers_never_offered_rate": 0.1}
    out = compare_policy_ab(actual, predicted)
    assert "matching_success_rate" in out["policy_improved_keys"]
    assert "passengers_never_offered_rate" in out["policy_improved_keys"]
    assert out["policy_net_improved"] is True
