"""Effort-tiered repair engine policy, budgets, and bundle layout."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from legolization.assembly_counterfactual import (
    CounterfactualCandidate,
    CounterfactualSearchResult,
)
from legolization.bundle.record import read_record
from legolization.catalog import catalog_hash, load_catalog
from legolization.configuration import ProjectConfig
from legolization.errors import ConfigurationError
from legolization.eval_artifacts import input_sha256
from legolization.layout import Layout
from legolization.ldraw_out import write_model
from legolization.main import main
from legolization.redesign import (
    RejectedCandidate,
    RepairCandidate,
    RepairSearchResult,
    search_repair,
)
from legolization.repair_bundle import (
    RepairPlan,
    RepairRequest,
    counterfactual_slice_s,
    effort_budget,
    ensure_never_overwrites,
    repair_bundle_record,
    resolve_repair_dir,
    run_repair,
)
from legolization.stability import SolverConfig

if TYPE_CHECKING:
    from pathlib import Path

    from legolization.repair_bundle import RepairDefect, RepairEffort

_PARITY = SolverConfig(torque_z=False, ground_pull=True)
_STRICT = SolverConfig(torque_z=True, ground_pull=True)


def _grounded_layout() -> Layout:
    layout = Layout(catalog=load_catalog())
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    return layout


def _input_model(tmp_path: Path) -> Path:
    path = tmp_path / "floating.ldr"
    write_model(_grounded_layout(), path)
    return path


def _cf_candidate(*, after_components: int = 1) -> CounterfactualCandidate:
    return CounterfactualCandidate(
        occurrence_id=1,
        source_model="floating.ldr",
        source_line=2,
        part_id="3005",
        kind="rotate",
        description="rotate local Y by 90 degrees",
        before_component_count=2,
        after_component_count=after_components,
        before_connection_count=0,
        after_connection_count=4,
        confirmed_connection_gain=4,
        component_reduction=1,
        confidence=0.9,
        model_text="0 repaired counterfactual\n",
    )


def _cf_result(
    candidate: CounterfactualCandidate | None = None,
) -> CounterfactualSearchResult:
    return CounterfactualSearchResult(
        status="found" if candidate is not None else "not_needed",
        candidate=candidate,
        tested_candidates=3,
        timed_out=False,
        elapsed_s=0.25,
        message="test counterfactual",
    )


def _redesign_result(
    *,
    candidate: RepairCandidate | None = None,
    timed_out: bool = False,
    complete: bool = True,
    error: str | None = None,
    best_rejected: RejectedCandidate | None = None,
) -> RepairSearchResult:
    return RepairSearchResult(
        layout=candidate.layout if candidate is not None else None,
        candidate=candidate,
        attempts=4,
        elapsed_seconds=0.5,
        timed_out=timed_out,
        complete=complete,
        error=error,
        best_rejected=best_rejected,
    )


def _repair_candidate() -> RepairCandidate:
    return RepairCandidate(
        layout=_grounded_layout(),
        tier="exterior-support",
        strict_capacity_n=0.98,
        parity_max_score=0.0,
        strict_max_score=0.0,
        added_signatures=(),
        removed_signatures=(),
        removed_brick_ids=(),
        added_cells=3,
        visible_added_cells=3,
    )


def _rejected_candidate() -> RejectedCandidate:
    return RejectedCandidate(
        layout=_grounded_layout(),
        tier="envelope-retile",
        failed_gate="connectivity",
        detail="candidate is not one grounded component",
        metrics={"added_cells": 0, "visible_added_cells": 0, "component_count": 2},
    )


def _request(  # noqa: PLR0913 - explicit test-request knobs
    tmp_path: Path,
    *,
    defect: RepairDefect = "topological",
    effort: RepairEffort = "balanced",
    time_budget_s: float | None = None,
    counterfactual: CounterfactualSearchResult | None = None,
    redesign: RepairSearchResult | None = None,
    counterfactual_budgets: list[float] | None = None,
    redesign_budgets: list[float] | None = None,
    after_verdict: str | None = None,
) -> RepairRequest:
    source = _input_model(tmp_path)
    bundle_dir = tmp_path / "floating-repair"
    plan = RepairPlan(
        effort=effort,
        time_budget_s=effort_budget(effort, time_budget_s),
    )

    def counterfactual_search(budget: float) -> CounterfactualSearchResult:
        if counterfactual_budgets is not None:
            counterfactual_budgets.append(budget)
        return counterfactual if counterfactual is not None else _cf_result()

    def redesign_search(budget: float) -> RepairSearchResult:
        if redesign_budgets is not None:
            redesign_budgets.append(budget)
        if redesign is None:
            pytest.fail("the redesign search must not run for this case")
        return redesign

    def after_analysis(path: Path) -> dict[str, object]:
        del path
        return {"verdict": after_verdict or "feasible"}

    return RepairRequest(
        input_path=source,
        plan=plan,
        bundle_dir=bundle_dir,
        record=repair_bundle_record(
            source,
            bundle_dir,
            input_sha=input_sha256(source),
            project=ProjectConfig(),
            catalog_sha256=catalog_hash(),
        ),
        defect=defect,
        before_payload={"verdict": "disconnected"},
        counterfactual_search=counterfactual_search,
        redesign_search=redesign_search,
        after_analysis=after_analysis,
    )


def test_effort_budgets_and_exhaustive_requirement():
    assert effort_budget("fast", None) == 60.0
    assert effort_budget("balanced", None) == 300.0
    assert effort_budget("fast", 10.0) == 10.0
    assert effort_budget("exhaustive", 900.0) == 900.0
    with pytest.raises(ConfigurationError, match="requires an explicit --time-budget"):
        effort_budget("exhaustive", None)


def test_counterfactual_slice_is_capped_quarter_budget():
    assert counterfactual_slice_s(300.0) == 60.0
    assert counterfactual_slice_s(60.0) == 15.0
    assert RepairPlan(
        effort="balanced", time_budget_s=300.0
    ).counterfactual_slice_s == (60.0)
    assert RepairPlan(effort="fast", time_budget_s=60.0).counterfactual_slice_s == 15.0


def test_sufficient_counterfactual_skips_redesign(tmp_path: Path):
    counterfactual_budgets: list[float] = []
    request = _request(
        tmp_path,
        counterfactual=_cf_result(_cf_candidate()),
        counterfactual_budgets=counterfactual_budgets,
    )
    original = request.input_path.read_bytes()

    result = run_repair(request)

    assert counterfactual_budgets == [60.0]
    assert result.exit_code == 0
    assert result.status == "repaired"
    assert request.input_path.read_bytes() == original
    model = request.bundle_dir / "model" / "floating.repaired.mpd"
    assert model.read_text() == "0 repaired counterfactual\n"
    payload = json.loads((request.bundle_dir / "repair.json").read_text())
    assert payload["schema"] == "legolization.repair/v1"
    assert payload["tier"] == "balanced"
    assert payload["status"] == "repaired"
    assert payload["strategy"] == "counterfactual"
    assert payload["verification"] == "physics-validated"
    assert payload["timings"]["counterfactual_slice_s"] == 60.0
    assert (request.bundle_dir / "analysis" / "before.json").is_file()
    assert (request.bundle_dir / "analysis" / "after.json").is_file()
    record = read_record(request.bundle_dir)
    assert record is not None
    assert record.status == "complete"
    assert record.exit_code == 0
    assert record.quality == "direct"
    assert {entry.path for entry in record.artifacts} >= {
        "repair.json",
        "model/floating.repaired.mpd",
        "analysis/before.json",
    }
    assert all(entry.sha256 is not None for entry in record.artifacts)


def test_physical_defect_escalates_to_redesign_with_remaining_budget(tmp_path: Path):
    redesign_budgets: list[float] = []
    request = _request(
        tmp_path,
        defect="physical",
        counterfactual=_cf_result(_cf_candidate()),
        redesign=_redesign_result(candidate=_repair_candidate()),
        redesign_budgets=redesign_budgets,
    )

    result = run_repair(request)

    assert len(redesign_budgets) == 1
    assert 290.0 < redesign_budgets[0] <= 300.0
    assert result.exit_code == 0
    assert result.status == "repaired"
    payload = json.loads((request.bundle_dir / "repair.json").read_text())
    assert payload["strategy"] == "redesign"
    assert payload["redesign"]["status"] == "found"
    assert payload["metrics"]["visible_added_cells"] == 3
    assert (request.bundle_dir / "model" / "floating.repaired.mpd").is_file()


def test_insufficient_counterfactual_escalates(tmp_path: Path):
    redesign_budgets: list[float] = []
    request = _request(
        tmp_path,
        counterfactual=_cf_result(_cf_candidate(after_components=2)),
        redesign=_redesign_result(candidate=_repair_candidate()),
        redesign_budgets=redesign_budgets,
    )

    result = run_repair(request)

    assert redesign_budgets
    assert result.status == "repaired"


def test_timed_out_repair_retains_best_rejected_and_exits_partial(tmp_path: Path):
    request = _request(
        tmp_path,
        redesign=_redesign_result(
            timed_out=True,
            complete=False,
            best_rejected=_rejected_candidate(),
        ),
    )

    result = run_repair(request)

    assert result.exit_code == 3
    assert result.status == "timed_out"
    assert "best rejected candidate is retained" in result.explanation
    assert (request.bundle_dir / "diagnostics" / "best-rejected.mpd").is_file()
    rejected = json.loads(
        (request.bundle_dir / "diagnostics" / "best-rejected.json").read_text()
    )
    assert rejected["failed_gate"] == "connectivity"
    assert rejected["tier"] == "envelope-retile"
    payload = json.loads((request.bundle_dir / "repair.json").read_text())
    assert payload["best_rejected"]["failed_gate"] == "connectivity"
    assert not (request.bundle_dir / "model").exists()
    record = read_record(request.bundle_dir)
    assert record is not None
    assert record.status == "partial"
    assert record.exit_code == 3


def test_exhausted_search_is_provably_nothing(tmp_path: Path):
    request = _request(tmp_path, redesign=_redesign_result())

    result = run_repair(request)

    assert result.exit_code == 2
    assert result.status == "exhausted"
    record = read_record(request.bundle_dir)
    assert record is not None
    assert record.status == "unbuildable"


def test_redesign_error_is_operational(tmp_path: Path):
    request = _request(
        tmp_path,
        redesign=_redesign_result(complete=False, error="worker exploded"),
    )

    result = run_repair(request)

    assert result.exit_code == 1
    assert result.status == "error"
    assert "worker exploded" in result.explanation


def test_no_defect_is_not_needed(tmp_path: Path):
    request = _request(tmp_path, defect="none")

    result = run_repair(request)

    assert result.exit_code == 0
    assert result.status == "not_needed"
    payload = json.loads((request.bundle_dir / "repair.json").read_text())
    assert payload["status"] == "not_needed"


def test_repair_never_overwrites_the_input(tmp_path: Path):
    source = _input_model(tmp_path)

    with pytest.raises(ConfigurationError, match="must not contain"):
        ensure_never_overwrites(source, tmp_path)
    with pytest.raises(ConfigurationError, match="must not contain"):
        resolve_repair_dir(source, tmp_path, input_sha=input_sha256(source))
    ensure_never_overwrites(source, tmp_path / "elsewhere")


def test_repair_dir_reuses_matching_bundle_and_numbers_siblings(tmp_path: Path):
    request = _request(tmp_path, counterfactual=_cf_result(_cf_candidate()))
    source = request.input_path
    input_sha = input_sha256(source)
    base = tmp_path / "floating-repair"

    assert resolve_repair_dir(source, None, input_sha=input_sha) == base
    run_repair(request)
    assert resolve_repair_dir(source, None, input_sha=input_sha) == base
    assert (
        resolve_repair_dir(source, None, input_sha="0" * 64)
        == tmp_path / "floating-repair-2"
    )


def test_analyze_repair_cli_writes_repair_bundle(tmp_path: Path):
    layout = Layout(catalog=load_catalog())
    layout.add("brick_1x1", 0, 0, 3, 0, 4)
    source = tmp_path / "floating.ldr"
    write_model(layout, source)
    original = source.read_bytes()

    code = main(
        [
            "analyze",
            str(source),
            "--preserve-origin",
            "--repair",
            "--effort",
            "fast",
            "--time-budget",
            "25",
        ]
    )

    assert code == 0
    assert source.read_bytes() == original
    bundle = tmp_path / "floating-repair"
    assert (bundle / "model" / "floating.repaired.mpd").is_file()
    payload = json.loads((bundle / "repair.json").read_text())
    assert payload["status"] == "repaired"
    assert payload["tier"] == "fast"
    assert payload["strategy"] == "redesign"
    assert (bundle / "analysis" / "before.json").is_file()
    record = read_record(bundle)
    assert record is not None
    assert record.exit_code == 0


@pytest.mark.parametrize(
    "argv",
    [
        ("--effort", "fast"),
        ("--repair-output", "somewhere"),
        ("--repair", "--effort", "exhaustive"),
    ],
)
def test_analyze_repair_usage_errors(tmp_path: Path, argv: tuple[str, ...]):
    source = _input_model(tmp_path)

    assert main(["analyze", str(source), *argv]) == 1


def test_analyze_repair_output_may_not_contain_the_input(tmp_path: Path):
    source = _input_model(tmp_path)

    code = main(
        [
            "analyze",
            str(source),
            "--repair",
            "--repair-output",
            str(tmp_path),
        ]
    )

    assert code == 1


def test_search_repair_streams_best_rejected_over_the_queue():
    layout = Layout(catalog=load_catalog())
    floater = layout.add("brick_1x1", 0, 0, 3, 0, 4)

    result = search_repair(
        layout,
        physics_seed_ids=(floater.brick_id,),
        parity_solver=_PARITY,
        strict_solver=_STRICT,
        time_budget_s=30.0,
        seed=0,
    )

    assert result.candidate is not None
    assert result.candidate.tier == "exterior-support"
    assert result.best_rejected is not None
    assert result.best_rejected.tier == "envelope-retile"
    assert result.best_rejected.metrics["added_cells"] == 0
    rejected = result.best_rejected.to_payload()
    assert rejected["failed_gate"] == result.best_rejected.failed_gate
    assert result.to_report({})["rejected"] == rejected
