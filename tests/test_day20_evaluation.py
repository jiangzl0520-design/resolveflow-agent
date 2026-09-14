from evaluation.run_day20_harness import main, run_evaluation


def test_day20_golden_evaluation_is_reproducible() -> None:
    first = run_evaluation()
    second = run_evaluation()

    assert first == second
    assert first["synthetic"] is True
    assert first["baseline"]["total"] == 20
    assert first["candidate"]["total"] == 20
    assert first["candidate"]["accuracy"] == 1.0
    assert first["comparison"]["regressions"] == []
    assert first["comparison"]["quality_gate_passed"] is True


def test_day20_cli_writes_report_and_uses_stable_exit_codes(tmp_path) -> None:
    output = tmp_path / "day20-report.json"

    assert main(["--output", str(output)]) == 0
    assert output.exists()
    assert main(["--minimum-accuracy", "1.1", "--output", str(output)]) == 2
