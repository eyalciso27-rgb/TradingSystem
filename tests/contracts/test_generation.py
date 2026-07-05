"""Generator determinism and drift-detection tests (IMP-0101).

Negative scenarios operate on temporary copies only; the tracked generated
files are never modified by these tests.
"""

from pathlib import Path
from types import ModuleType

import pytest


def test_rendering_is_deterministic(generator: ModuleType) -> None:
    first = generator.render_typescript()
    second = generator.render_typescript()
    assert first == second
    for enum_class in generator.CANONICAL_ENUMS:
        assert generator.render_schema(enum_class) == generator.render_schema(enum_class)
    assert generator.render_envelope_schema() == generator.render_envelope_schema()
    assert generator.render_envelope_typescript() == generator.render_envelope_typescript()


def test_no_timestamps_in_generated_output(generator: ModuleType) -> None:
    text = generator.render_typescript()
    assert "20" + "26" not in text  # no generation date
    for enum_class in generator.CANONICAL_ENUMS:
        assert '"x-generated-note": "GENERATED FILE' in generator.render_schema(enum_class)


def test_tracked_generated_files_have_no_drift(generator: ModuleType) -> None:
    assert generator.check_files() == []


def _redirect_to_tmp(
    generator: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(generator, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(generator, "SCHEMA_DIR", tmp_path / "schemas" / "enums")
    monkeypatch.setattr(generator, "EVENTS_SCHEMA_DIR", tmp_path / "schemas" / "events")
    monkeypatch.setattr(generator, "TS_FILE", tmp_path / "ts" / "enums.ts")
    monkeypatch.setattr(generator, "ENVELOPE_TS_FILE", tmp_path / "ts" / "event-envelope.ts")


def test_missing_generated_file_is_detected(
    generator: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect_to_tmp(generator, monkeypatch, tmp_path)
    generator.write_files()
    (tmp_path / "ts" / "enums.ts").unlink()
    problems = generator.check_files()
    assert any(problem.startswith("missing-generated:") for problem in problems)


def test_manual_edit_of_generated_file_is_detected(
    generator: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect_to_tmp(generator, monkeypatch, tmp_path)
    generator.write_files()
    ts_file = tmp_path / "ts" / "enums.ts"
    ts_file.write_text(
        ts_file.read_text(encoding="utf-8").replace('"REJECT"', '"HAND_EDITED"'),
        encoding="utf-8",
    )
    problems = generator.check_files()
    assert any(problem.startswith("drift:") for problem in problems)


def test_schema_drift_is_detected(
    generator: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect_to_tmp(generator, monkeypatch, tmp_path)
    generator.write_files()
    schema = tmp_path / "schemas" / "enums" / "decision_outcome.schema.json"
    schema.write_text(
        schema.read_text(encoding="utf-8").replace('"REJECT"', '"REJECTED_TYPO"'),
        encoding="utf-8",
    )
    problems = generator.check_files()
    assert any(problem.startswith("drift:") for problem in problems)


def test_stray_schema_file_is_detected(
    generator: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect_to_tmp(generator, monkeypatch, tmp_path)
    generator.write_files()
    stray = tmp_path / "schemas" / "enums" / "invented_enum.schema.json"
    stray.write_text("{}", encoding="utf-8")
    problems = generator.check_files()
    assert any(problem.startswith("stray-generated:") for problem in problems)


def test_stray_event_schema_is_detected(
    generator: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect_to_tmp(generator, monkeypatch, tmp_path)
    generator.write_files()
    stray = tmp_path / "schemas" / "events" / "invented_event.schema.json"
    stray.write_text("{}", encoding="utf-8")
    problems = generator.check_files()
    assert any(problem.startswith("stray-generated:") for problem in problems)


def test_envelope_drift_is_detected(
    generator: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect_to_tmp(generator, monkeypatch, tmp_path)
    generator.write_files()
    envelope_ts = tmp_path / "ts" / "event-envelope.ts"
    envelope_ts.write_text(
        envelope_ts.read_text(encoding="utf-8").replace("correlation_id", "renamed_field"),
        encoding="utf-8",
    )
    problems = generator.check_files()
    assert any(problem.startswith("drift:") for problem in problems)


def test_missing_envelope_schema_is_detected(
    generator: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect_to_tmp(generator, monkeypatch, tmp_path)
    generator.write_files()
    (tmp_path / "schemas" / "events" / "event_envelope.schema.json").unlink()
    problems = generator.check_files()
    assert any(problem.startswith("missing-generated:") for problem in problems)


def test_write_files_is_idempotent(
    generator: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect_to_tmp(generator, monkeypatch, tmp_path)
    first = generator.write_files()
    second = generator.write_files()
    assert len(first) == len(generator.expected_files())
    assert second == []
