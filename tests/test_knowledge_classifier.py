"""Synthetic contract tests for sealed knowledge classification."""

from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from dataclasses import fields

import pytest

from agam.knowledge_classifier import (
    MIN_CONFIDENCE,
    MAX_MODEL_RESPONSE_CHARS,
    ClaudeCliRunner,
    ClassificationRequestItem,
    ClassificationRunSummary,
    ClassificationResult,
    ClassifierContractError,
    ClassifierProgressEvent,
    KnowledgeKind,
    PrivacyLabel,
    ProgressStatus,
    build_classifier_prompt,
    classify_graph,
    make_batch_id,
    make_opaque_id,
    parse_classifier_response,
    sha256_file,
)


BATCH_ID = "batch_" + "01" * 12
SECOND_BATCH_ID = "batch_" + "17" * 12


def _item(
    opaque_id: str,
    *,
    privacy: str = "PORTABLE",
    kind: str = "GUIDANCE",
    confidence: object = 0.95,
    reason_code: object = "portable_preference",
) -> dict[str, object]:
    return {
        "id": opaque_id,
        "privacy": privacy,
        "kind": kind,
        "confidence": confidence,
        "reason_code": reason_code,
    }


def _response(*items: dict[str, object], **extra: object) -> str:
    return json.dumps({"items": list(items), **extra})


def test_labels_are_closed_immutable_enum_values():
    assert tuple(label.value for label in PrivacyLabel) == (
        "PORTABLE",
        "RESTRICTED",
        "REVIEW",
    )
    assert tuple(kind.value for kind in KnowledgeKind) == (
        "GUIDANCE",
        "SOLUTION",
        "CUSTOM",
        "REVIEW",
    )
    assert MIN_CONFIDENCE == 0.80


def test_batch_ids_are_internally_generated_opaque_nonces(monkeypatch):
    monkeypatch.setattr(
        "agam.knowledge_classifier.secrets.token_hex", lambda size: "ab" * size
    )

    assert make_batch_id() == "batch_" + "ab" * 12


def test_semantic_batch_ids_are_rejected_from_ids_and_progress():
    with pytest.raises(ClassifierContractError):
        make_opaque_id("customer_alpha", 0)
    with pytest.raises(ClassifierContractError):
        ClassifierProgressEvent(
            batch_id="customer_alpha",
            status=ProgressStatus.STARTED,
            total=1,
            accepted=0,
            review=0,
            failed=0,
        )


def test_opaque_ids_are_deterministic_and_do_not_embed_source_names():
    opaque_id = make_opaque_id(SECOND_BATCH_ID, 4)

    assert opaque_id == make_opaque_id(SECOND_BATCH_ID, 4)
    assert opaque_id != make_opaque_id(SECOND_BATCH_ID, 5)
    assert opaque_id.startswith("item_")
    assert SECOND_BATCH_ID not in opaque_id
    assert "customer-alpha" not in opaque_id


def test_prompt_uses_only_opaque_ids_and_demands_exact_json_contract():
    opaque_id = make_opaque_id(BATCH_ID, 0)
    prompt = build_classifier_prompt(
        [ClassificationRequestItem(opaque_id, "synthetic portable preference")]
    )

    assert opaque_id in prompt
    assert "synthetic portable preference" in prompt
    assert "strict JSON only" in prompt
    assert "exactly these fields" in prompt
    assert "id, privacy, kind, confidence, reason_code" in prompt
    assert "free-form rationale" in prompt
    assert "entity_name" not in prompt


def test_valid_response_is_parsed_and_returned_in_expected_id_order():
    first = make_opaque_id(BATCH_ID, 0)
    second = make_opaque_id(BATCH_ID, 1)

    parsed = parse_classifier_response(
        _response(
            _item(second, privacy="RESTRICTED", kind="CUSTOM"),
            _item(first),
        ),
        [first, second],
    )

    assert parsed == (
        ClassificationResult(
            id=first,
            privacy=PrivacyLabel.PORTABLE,
            kind=KnowledgeKind.GUIDANCE,
            confidence=0.95,
            reason_code="portable_preference",
        ),
        ClassificationResult(
            id=second,
            privacy=PrivacyLabel.RESTRICTED,
            kind=KnowledgeKind.CUSTOM,
            confidence=0.95,
            reason_code="portable_preference",
        ),
    )


@pytest.mark.parametrize("confidence", [0, 0.799999])
def test_low_confidence_is_forced_to_review(confidence):
    opaque_id = make_opaque_id(BATCH_ID, 0)

    (result,) = parse_classifier_response(
        _response(_item(opaque_id, confidence=confidence)), [opaque_id]
    )

    assert result.privacy is PrivacyLabel.REVIEW
    assert result.kind is KnowledgeKind.REVIEW


@pytest.mark.parametrize(
    ("privacy", "kind"),
    [("REVIEW", "GUIDANCE"), ("PORTABLE", "REVIEW")],
)
def test_one_sided_review_is_contradictory_and_forced_to_full_review(privacy, kind):
    opaque_id = make_opaque_id(BATCH_ID, 0)

    (result,) = parse_classifier_response(
        _response(_item(opaque_id, privacy=privacy, kind=kind)), [opaque_id]
    )

    assert result.privacy is PrivacyLabel.REVIEW
    assert result.kind is KnowledgeKind.REVIEW


def test_full_review_from_model_remains_review():
    opaque_id = make_opaque_id(BATCH_ID, 0)

    (result,) = parse_classifier_response(
        _response(_item(opaque_id, privacy="REVIEW", kind="REVIEW")), [opaque_id]
    )

    assert result.privacy is PrivacyLabel.REVIEW
    assert result.kind is KnowledgeKind.REVIEW


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"items": [], "extra": True},
        {"items": "not-a-list"},
        {"items": [{"id": "only-one-field"}]},
    ],
)
def test_response_shape_is_exact(payload):
    opaque_id = make_opaque_id(BATCH_ID, 0)

    with pytest.raises(ClassifierContractError):
        parse_classifier_response(json.dumps(payload), [opaque_id])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item.update(extra="forbidden"),
        lambda item: item.pop("reason_code"),
        lambda item: item.update(privacy="UNKNOWN"),
        lambda item: item.update(kind="UNKNOWN"),
        lambda item: item.update(confidence=True),
        lambda item: item.update(confidence="0.95"),
        lambda item: item.update(confidence=-0.1),
        lambda item: item.update(confidence=1.1),
        lambda item: item.update(reason_code="UPPERCASE"),
        lambda item: item.update(reason_code="contains spaces"),
        lambda item: item.update(reason_code="x" * 65),
        lambda item: item.update(reason_code=7),
        lambda item: item.update(id=7),
    ],
)
def test_item_fields_and_types_are_strict(mutate):
    opaque_id = make_opaque_id(BATCH_ID, 0)
    item = _item(opaque_id)
    mutate(item)

    with pytest.raises(ClassifierContractError):
        parse_classifier_response(_response(item), [opaque_id])


def test_response_requires_exactly_one_item_per_expected_id():
    first = make_opaque_id(BATCH_ID, 0)
    second = make_opaque_id(BATCH_ID, 1)
    extra = make_opaque_id(BATCH_ID, 2)

    invalid_responses = (
        _response(_item(first)),
        _response(_item(first), _item(first)),
        _response(_item(first), _item(second), _item(extra)),
    )
    for raw in invalid_responses:
        with pytest.raises(ClassifierContractError):
            parse_classifier_response(raw, [first, second])


def test_malformed_json_error_does_not_echo_raw_model_response():
    raw_secret = 'not-json-SYNTHETIC_SECRET_RESPONSE'
    opaque_id = make_opaque_id(BATCH_ID, 0)

    with pytest.raises(ClassifierContractError) as error:
        parse_classifier_response(raw_secret, [opaque_id])

    assert raw_secret not in str(error.value)
    assert raw_secret not in repr(error.value)
    assert error.value.args == ("invalid_json",)


def test_exact_json_code_fence_is_accepted_but_surrounding_prose_is_not():
    opaque_id = make_opaque_id(BATCH_ID, 0)
    payload = _response(_item(opaque_id))

    assert parse_classifier_response(
        f"```json\n{payload}\n```", [opaque_id]
    )[0].id == opaque_id
    with pytest.raises(ClassifierContractError) as error:
        parse_classifier_response(
            f"Here is the result:\n```json\n{payload}\n```", [opaque_id]
        )
    assert error.value.code == "invalid_json"


def test_duplicate_json_keys_are_rejected_without_echoing_response():
    opaque_id = make_opaque_id(BATCH_ID, 0)
    raw = '{"items":[{"id":"%s","id":"%s"}]}' % (opaque_id, opaque_id)

    with pytest.raises(ClassifierContractError) as error:
        parse_classifier_response(raw, [opaque_id])

    assert error.value.code == "invalid_json"
    assert raw not in str(error.value)


@pytest.mark.parametrize("confidence", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_json_constants_are_rejected(confidence):
    opaque_id = make_opaque_id(BATCH_ID, 0)

    with pytest.raises(ClassifierContractError) as error:
        parse_classifier_response(
            _response(_item(opaque_id, confidence=confidence)), [opaque_id]
        )

    assert error.value.code == "invalid_json"


def test_overflowing_json_number_is_rejected_as_invalid_confidence():
    opaque_id = make_opaque_id(BATCH_ID, 0)
    raw = _response(_item(opaque_id)).replace("0.95", "1e999")

    with pytest.raises(ClassifierContractError) as error:
        parse_classifier_response(raw, [opaque_id])

    assert error.value.code == "invalid_confidence"


def test_huge_json_integer_is_rejected_without_overflow_or_echo():
    opaque_id = make_opaque_id(BATCH_ID, 0)
    huge_integer = "9" * 4000
    raw = _response(_item(opaque_id)).replace("0.95", huge_integer)

    with pytest.raises(ClassifierContractError) as error:
        parse_classifier_response(raw, [opaque_id])

    assert error.value.code == "invalid_confidence"
    assert huge_integer not in str(error.value)


def test_request_validation_error_does_not_echo_raw_input():
    raw_secret = "SYNTHETIC_SECRET_INPUT"

    with pytest.raises(ClassifierContractError) as error:
        ClassificationRequestItem("not-an-opaque-id", raw_secret)

    assert raw_secret not in str(error.value)
    assert raw_secret not in repr(error.value)


def test_prompt_validation_error_does_not_echo_raw_input():
    raw_secret = "SYNTHETIC_SECRET_INPUT"

    with pytest.raises(ClassifierContractError) as error:
        build_classifier_prompt([raw_secret])

    assert raw_secret not in str(error.value)
    assert raw_secret not in repr(error.value)


def test_progress_events_can_only_hold_opaque_aggregate_metadata():
    event = ClassifierProgressEvent(
        batch_id=BATCH_ID,
        status=ProgressStatus.COMPLETED,
        total=5,
        accepted=3,
        review=2,
        failed=0,
    )

    assert {field.name for field in fields(event)} == {
        "batch_id",
        "status",
        "total",
        "accepted",
        "review",
        "failed",
    }
    assert "SYNTHETIC_SECRET" not in repr(event)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"batch_id": "raw content with spaces"},
        {"total": True},
        {"accepted": -1},
        {"total": 1, "accepted": 2},
        {"total": 1, "accepted": 1, "review": 1},
    ],
)
def test_progress_event_rejects_unsafe_or_inconsistent_metadata(kwargs):
    values = {
        "batch_id": BATCH_ID,
        "status": ProgressStatus.COMPLETED,
        "total": 1,
        "accepted": 1,
        "review": 0,
        "failed": 0,
    }
    values.update(kwargs)

    with pytest.raises(ClassifierContractError):
        ClassifierProgressEvent(**values)


def _seed_source(path, *, extra_neighbors=0):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            type TEXT NOT NULL,
            description TEXT DEFAULT '',
            created TEXT NOT NULL,
            updated TEXT NOT NULL,
            last_referenced TEXT
        );
        CREATE TABLE relationships (
            id INTEGER PRIMARY KEY,
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            relation TEXT NOT NULL,
            weight REAL DEFAULT 1.0,
            created TEXT NOT NULL,
            UNIQUE(source_id, target_id, relation)
        );
        CREATE TABLE properties (
            id INTEGER PRIMARY KEY,
            entity_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            updated TEXT NOT NULL,
            UNIQUE(entity_id, key)
        );
        """
    )
    timestamp = "2026-01-01T00:00:00Z"
    conn.executemany(
        "INSERT INTO entities(id,name,type,description,created,updated) "
        "VALUES(?,?,?,?,?,?)",
        [
            (1, "synthetic-portable", "preference", "PORTABLE_MARKER", timestamp, timestamp),
            (2, "synthetic-private", "lesson", "PRIVATE_MARKER", timestamp, timestamp),
            (3, "synthetic-work", "lesson", "WORK_MARKER", timestamp, timestamp),
        ],
    )
    conn.execute(
        "INSERT INTO properties(id,entity_id,key,value,updated) VALUES(1,1,?,?,?)",
        ("synthetic-key", "PROPERTY_MARKER", timestamp),
    )
    conn.execute(
        "INSERT INTO relationships(id,source_id,target_id,relation,created) "
        "VALUES(1,1,2,?,?)",
        ("synthetic-link", timestamp),
    )
    for offset in range(extra_neighbors):
        entity_id = 10 + offset
        conn.execute(
            "INSERT INTO entities(id,name,type,description,created,updated) "
            "VALUES(?,?,?,?,?,?)",
            (
                entity_id,
                f"neighbor-{offset}",
                "context",
                f"NEIGHBOR_{offset}",
                timestamp,
                timestamp,
            ),
        )
        conn.execute(
            "INSERT INTO relationships(id,source_id,target_id,relation,created) "
            "VALUES(?,?,?,?,?)",
            (10 + offset, 1, entity_id, "bounded-link", timestamp),
        )
    conn.commit()
    conn.close()


def _prompt_items(prompt):
    return json.loads(prompt.split("INPUT_JSON:\n", 1)[1])["items"]


def _with_model(runner, model="haiku"):
    runner.model = model
    return runner


def _classifying_runner(prompts):
    def run(prompt):
        prompts.append(prompt)
        results = []
        for item in _prompt_items(prompt):
            content = item["content"]
            entity_description = json.loads(content)["entity"]["description"]
            if "PORTABLE_MARKER" in entity_description:
                privacy, kind = "PORTABLE", "GUIDANCE"
            elif "PRIVATE_MARKER" in entity_description:
                privacy, kind = "RESTRICTED", "CUSTOM"
            else:
                privacy, kind = "RESTRICTED", "SOLUTION"
            results.append(
                _item(
                    item["id"],
                    privacy=privacy,
                    kind=kind,
                    reason_code="synthetic_route",
                )
            )
        return _response(*results)

    return _with_model(run)


def test_classify_graph_uses_backup_context_prefixes_and_preserves_source(tmp_path):
    source = tmp_path / "source.db"
    staging = tmp_path / "sealed" / "staging.db"
    _seed_source(source, extra_neighbors=4)
    source_hash = sha256_file(source)
    prompts = []

    summary = classify_graph(
        source,
        staging,
        _classifying_runner(prompts),
        batch_size=8,
        neighbor_limit=8,
    )

    assert isinstance(summary, ClassificationRunSummary)
    assert summary.source_sha256 == source_hash == sha256_file(source)
    assert summary.total == 7
    assert summary.accepted == 7
    assert summary.review == 0
    assert summary.failed == 0
    assert staging.exists()
    joined_prompts = "\n".join(prompts)
    assert "PROPERTY_MARKER" in joined_prompts
    assert "PRIVATE_MARKER" in joined_prompts
    portable_content = next(
        item["content"]
        for prompt in prompts
        for item in _prompt_items(prompt)
        if "PORTABLE_MARKER" in item["content"]
    )
    assert portable_content.count("NEIGHBOR_") == 4

    conn = sqlite3.connect(staging)
    descriptions = dict(conn.execute("SELECT id,description FROM entities"))
    rows = conn.execute(
        "SELECT entity_id,privacy,kind,confidence,reason_code,failed "
        "FROM agam_classifications ORDER BY entity_id"
    ).fetchall()
    conn.close()
    assert descriptions[1].startswith("[PORTABLE]")
    assert descriptions[2].startswith("[RESTRICTED]")
    assert descriptions[3].startswith("[RESTRICTED]")
    assert rows[0][1:] == (
        "PORTABLE",
        "GUIDANCE",
        0.95,
        "synthetic_route",
        0,
    )


def test_retry_review_item_reclassifies_only_selected_opaque_id(tmp_path):
    source = tmp_path / "source.db"
    staging = tmp_path / "staging.db"
    _seed_source(source)

    def first_pass(prompt):
        output = []
        for item in _prompt_items(prompt):
            content = json.loads(item["content"])["entity"]["description"]
            if "PORTABLE_MARKER" in content:
                output.append(
                    _item(
                        item["id"],
                        privacy="REVIEW",
                        kind="REVIEW",
                        confidence=0.0,
                        reason_code="ambiguous_scope",
                    )
                )
            else:
                output.append(
                    _item(
                        item["id"],
                        privacy="RESTRICTED",
                        kind="CUSTOM",
                        reason_code="synthetic_route",
                    )
                )
        return _response(*output)

    classify_graph(source, staging, _with_model(first_pass))
    conn = sqlite3.connect(staging)
    opaque_id = conn.execute(
        "SELECT opaque_id FROM agam_classifications WHERE privacy='REVIEW'"
    ).fetchone()[0]
    conn.close()

    prompts = []

    def second_pass(prompt):
        prompts.append(prompt)
        item = _prompt_items(prompt)[0]
        return _response(
            _item(
                item["id"],
                privacy="PORTABLE",
                kind="SOLUTION",
                reason_code="user_confirmed",
            )
        )

    summary = classify_graph(
        source,
        staging,
        _with_model(second_pass),
        retry_review_id=opaque_id,
    )

    assert len(prompts) == 1
    assert [item["id"] for item in _prompt_items(prompts[0])] == [opaque_id]
    assert summary.review == 0
    conn = sqlite3.connect(staging)
    row = conn.execute(
        "SELECT privacy,kind,reason_code,failed FROM agam_classifications "
        "WHERE opaque_id=?",
        (opaque_id,),
    ).fetchone()
    description = conn.execute(
        "SELECT description FROM entities WHERE id=1"
    ).fetchone()[0]
    conn.close()
    assert row == ("PORTABLE", "SOLUTION", "user_confirmed", 0)
    assert description.startswith("[PORTABLE]")


def test_retry_review_item_rejects_non_review_row(tmp_path):
    source = tmp_path / "source.db"
    staging = tmp_path / "staging.db"
    _seed_source(source)
    classify_graph(source, staging, _classifying_runner([]))
    conn = sqlite3.connect(staging)
    opaque_id = conn.execute(
        "SELECT opaque_id FROM agam_classifications ORDER BY entity_id LIMIT 1"
    ).fetchone()[0]
    conn.close()

    with pytest.raises(ClassifierContractError, match="invalid_review_item"):
        classify_graph(
            source,
            staging,
            _classifying_runner([]),
            retry_review_id=opaque_id,
        )


def test_classify_graph_can_bound_parallel_model_calls(tmp_path):
    source = tmp_path / "source.db"
    staging = tmp_path / "staging.db"
    _seed_source(source)
    barrier = threading.Barrier(3)
    lock = threading.Lock()
    active = 0
    peak = 0
    normal = _classifying_runner([])

    def concurrent_runner(prompt):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            barrier.wait(timeout=2)
            return normal(prompt)
        finally:
            with lock:
                active -= 1

    summary = classify_graph(
        source,
        staging,
        _with_model(concurrent_runner),
        batch_size=1,
        parallelism=3,
    )

    assert summary.total == 3
    assert summary.failed == 0
    assert peak == 3


def test_omitted_late_property_forces_review_without_sending_incomplete_entity(
    tmp_path,
):
    source = tmp_path / "source.db"
    staging = tmp_path / "staging.db"
    _seed_source(source)
    conn = sqlite3.connect(source)
    conn.executemany(
        "INSERT INTO properties(id,entity_id,key,value,updated) VALUES(?,?,?,?,?)",
        [
            (100 + offset, 1, f"property-{offset:02d}", "benign", "2026-01-01T00:00:00Z")
            for offset in range(32)
        ]
        + [
            (
                132,
                1,
                "zz-restricted",
                "SYNTHETIC_RESTRICTED_LATE_MARKER",
                "2026-01-01T00:00:00Z",
            )
        ],
    )
    conn.commit()
    conn.close()
    prompts = []

    summary = classify_graph(source, staging, _classifying_runner(prompts))

    conn = sqlite3.connect(staging)
    row = conn.execute(
        "SELECT privacy,kind,confidence,reason_code,failed "
        "FROM agam_classifications WHERE entity_id=1"
    ).fetchone()
    conn.close()
    assert row == ("REVIEW", "REVIEW", 0.0, "input_incomplete", 0)
    assert summary.review == 1
    assert "SYNTHETIC_RESTRICTED_LATE_MARKER" not in "\n".join(prompts)


def test_oversized_publishable_content_is_not_sent_as_truncated_json(tmp_path):
    source = tmp_path / "source.db"
    staging = tmp_path / "staging.db"
    _seed_source(source)
    conn = sqlite3.connect(source)
    conn.execute("DELETE FROM relationships")
    conn.execute("DELETE FROM properties")
    conn.execute("DELETE FROM entities WHERE id != 1")
    conn.execute(
        "UPDATE entities SET description=? WHERE id=1",
        ("X" * 40_000,),
    )
    conn.commit()
    conn.close()
    runner_called = False

    def runner(_prompt):
        nonlocal runner_called
        runner_called = True
        raise AssertionError("incomplete input must not be sent")

    classify_graph(source, staging, _with_model(runner))

    conn = sqlite3.connect(staging)
    row = conn.execute(
        "SELECT privacy,kind,reason_code,failed "
        "FROM agam_classifications WHERE entity_id=1"
    ).fetchone()
    conn.close()
    assert runner_called is False
    assert row == ("REVIEW", "REVIEW", "input_incomplete", 0)


def test_classify_graph_resumes_without_resending_completed_entities(tmp_path):
    source = tmp_path / "source.db"
    staging = tmp_path / "staging.db"
    _seed_source(source)
    first_prompts = []
    normal = _classifying_runner(first_prompts)
    calls = 0

    def interrupt_second_batch(prompt):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return normal(prompt)

    with pytest.raises(KeyboardInterrupt):
        classify_graph(
            source, staging, _with_model(interrupt_second_batch), batch_size=1
        )

    conn = sqlite3.connect(staging)
    checkpoint_seal = conn.execute(
        "SELECT schema_version,algorithm,signature FROM agam_run_integrity"
    ).fetchone()
    conn.close()
    assert checkpoint_seal is not None
    assert checkpoint_seal[:2] == ("1", "hmac-sha256")

    resumed_prompts = []
    summary = classify_graph(
        source, staging, _classifying_runner(resumed_prompts), batch_size=8
    )
    assert summary.total == 3
    assert len(_prompt_items(first_prompts[0])) == 1
    assert sum(len(_prompt_items(prompt)) for prompt in resumed_prompts) == 2


@pytest.mark.parametrize(
    ("statement", "parameters"),
    [
        ("UPDATE entities SET name=? WHERE id=2", ("forged-pending-name",)),
        (
            "UPDATE properties SET value=? WHERE entity_id=2",
            ("forged-pending-property",),
        ),
    ],
)
def test_resume_rejects_pending_publishable_tamper_after_interruption(
    tmp_path, statement, parameters
):
    source = tmp_path / "source.db"
    staging = tmp_path / "staging.db"
    _seed_source(source)
    conn = sqlite3.connect(source)
    conn.execute(
        "INSERT INTO properties(id,entity_id,key,value,updated) VALUES(2,2,?,?,?)",
        ("pending-key", "PENDING_PROPERTY", "2026-01-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()
    normal = _classifying_runner([])
    calls = 0

    def interrupt_second_batch(prompt):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return normal(prompt)

    with pytest.raises(KeyboardInterrupt):
        classify_graph(
            source, staging, _with_model(interrupt_second_batch), batch_size=1
        )

    conn = sqlite3.connect(staging)
    conn.execute(statement, parameters)
    conn.commit()
    conn.close()

    with pytest.raises(ClassifierContractError) as error:
        classify_graph(source, staging, _classifying_runner([]))

    assert error.value.code == "staging_tampered"


def test_resume_rejects_completed_updated_tamper_after_interruption(tmp_path):
    source = tmp_path / "source.db"
    staging = tmp_path / "staging.db"
    _seed_source(source)
    normal = _classifying_runner([])
    calls = 0

    def interrupt_second_batch(prompt):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return normal(prompt)

    with pytest.raises(KeyboardInterrupt):
        classify_graph(
            source, staging, _with_model(interrupt_second_batch), batch_size=1
        )

    conn = sqlite3.connect(staging)
    conn.execute(
        "UPDATE entities SET updated=? WHERE id=1",
        ("2026-01-02T00:00:00Z",),
    )
    conn.commit()
    conn.close()

    with pytest.raises(ClassifierContractError) as error:
        classify_graph(source, staging, _classifying_runner([]))

    assert error.value.code == "staging_tampered"


@pytest.mark.parametrize(
    ("runner_factory", "reason_code", "failed"),
    [
        (lambda: (lambda _prompt: (_ for _ in ()).throw(RuntimeError("RAW_SECRET"))), "runner_failed", 1),
        (lambda: (lambda _prompt: "RAW_SECRET invalid json"), "invalid_model_response", 1),
        (
            lambda: (
                lambda prompt: _response(
                    *[
                        _item(item["id"], confidence=0.2, reason_code="uncertain")
                        for item in _prompt_items(prompt)
                    ]
                )
            ),
            "uncertain",
            0,
        ),
    ],
)
def test_classifier_failures_and_uncertainty_route_to_review(
    tmp_path, capsys, runner_factory, reason_code, failed
):
    source = tmp_path / "source.db"
    staging = tmp_path / "staging.db"
    _seed_source(source)

    summary = classify_graph(
        source, staging, _with_model(runner_factory()), batch_size=8
    )

    captured = capsys.readouterr()
    assert "RAW_SECRET" not in captured.out + captured.err
    conn = sqlite3.connect(staging)
    rows = conn.execute(
        "SELECT privacy,kind,reason_code,failed FROM agam_classifications"
    ).fetchall()
    descriptions = [row[0] for row in conn.execute("SELECT description FROM entities")]
    conn.close()
    assert all(row[:3] == ("REVIEW", "REVIEW", reason_code) for row in rows)
    assert all(row[3] == failed for row in rows)
    assert all(value.startswith("[REVIEW]") for value in descriptions)
    assert summary.review == 3
    assert summary.failed == 3 * failed


def test_retry_failed_reclassifies_only_failed_rows_from_immutable_source(tmp_path):
    source = tmp_path / "source.db"
    staging = tmp_path / "staging.db"
    _seed_source(source)

    failed = classify_graph(
        source,
        staging,
        _with_model(lambda _prompt: (_ for _ in ()).throw(RuntimeError())),
        batch_size=8,
    )
    assert failed.failed == 3

    prompts = []
    retried = classify_graph(
        source,
        staging,
        _classifying_runner(prompts),
        batch_size=2,
        parallelism=2,
        retry_failed=True,
    )

    assert retried.failed == 0
    assert retried.accepted == 3
    assert sum(len(_prompt_items(prompt)) for prompt in prompts) == 3


def test_classify_graph_detects_source_drift_without_raw_diagnostics(tmp_path):
    source = tmp_path / "source.db"
    staging = tmp_path / "staging.db"
    _seed_source(source)

    def drifting_runner(prompt):
        conn = sqlite3.connect(source)
        conn.execute("UPDATE entities SET description='DRIFT_MARKER' WHERE id=1")
        conn.commit()
        conn.close()
        return _classifying_runner([])(prompt)

    with pytest.raises(ClassifierContractError) as error:
        classify_graph(source, staging, _with_model(drifting_runner))

    assert error.value.code == "source_changed"
    assert "DRIFT_MARKER" not in str(error.value)


def test_classify_graph_rejects_byte_identical_source_swap_during_runner(tmp_path):
    source = tmp_path / "source.db"
    replacement = tmp_path / "replacement.db"
    staging = tmp_path / "staging.db"
    _seed_source(source)
    shutil.copyfile(source, replacement)
    normal = _classifying_runner([])
    swapped = False

    def swapping_runner(prompt):
        nonlocal swapped
        if not swapped:
            os.replace(replacement, source)
            swapped = True
        return normal(prompt)

    with pytest.raises(ClassifierContractError) as error:
        classify_graph(source, staging, _with_model(swapping_runner))

    assert swapped is True
    assert error.value.code == "source_changed"


def test_classify_graph_rejects_byte_identical_source_swap_on_resume(tmp_path):
    source = tmp_path / "source.db"
    replacement = tmp_path / "replacement.db"
    staging = tmp_path / "staging.db"
    _seed_source(source)
    normal = _classifying_runner([])
    calls = 0

    def interrupt_second_batch(prompt):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return normal(prompt)

    with pytest.raises(KeyboardInterrupt):
        classify_graph(
            source, staging, _with_model(interrupt_second_batch), batch_size=1
        )
    shutil.copyfile(source, replacement)
    os.replace(replacement, source)

    with pytest.raises(ClassifierContractError) as error:
        classify_graph(source, staging, _classifying_runner([]))

    assert error.value.code == "source_changed"


def test_classify_graph_rejects_byte_identical_source_swap_after_last_batch(tmp_path):
    source = tmp_path / "source.db"
    replacement = tmp_path / "replacement.db"
    staging = tmp_path / "staging.db"
    _seed_source(source)
    shutil.copyfile(source, replacement)
    swapped = False

    def swap_after_completed_batch(event):
        nonlocal swapped
        if event.status is ProgressStatus.COMPLETED and not swapped:
            os.replace(replacement, source)
            swapped = True

    with pytest.raises(ClassifierContractError) as error:
        classify_graph(
            source,
            staging,
            _classifying_runner([]),
            event_sink=swap_after_completed_batch,
        )

    assert swapped is True
    assert error.value.code == "source_changed"


@pytest.mark.parametrize("alias_kind", ["same", "symlink", "hardlink"])
def test_classify_graph_rejects_source_staging_identity_before_write(
    tmp_path, alias_kind
):
    source = tmp_path / "source.db"
    _seed_source(source)
    original_hash = sha256_file(source)
    if alias_kind == "same":
        staging = source
    elif alias_kind == "symlink":
        staging = tmp_path / "staging.db"
        staging.symlink_to(source)
    else:
        staging = tmp_path / "staging.db"
        staging.hardlink_to(source)

    with pytest.raises(ClassifierContractError) as error:
        classify_graph(source, staging, _classifying_runner([]))

    assert error.value.code == "source_staging_conflict"
    assert sha256_file(source) == original_hash
    conn = sqlite3.connect(source)
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='agam_classifier_meta'"
    ).fetchone()[0] == 0
    conn.close()


def test_existing_staging_without_classifier_provenance_is_rejected(tmp_path):
    source = tmp_path / "source.db"
    staging = tmp_path / "unrelated.db"
    _seed_source(source)
    _seed_source(staging)
    original_staging_hash = sha256_file(staging)

    with pytest.raises(ClassifierContractError) as error:
        classify_graph(source, staging, _classifying_runner([]))

    assert error.value.code == "staging_mismatch"
    assert sha256_file(staging) == original_staging_hash


def test_resume_rejects_tampered_completed_classification(tmp_path):
    source = tmp_path / "source.db"
    staging = tmp_path / "staging.db"
    _seed_source(source)
    classify_graph(source, staging, _classifying_runner([]))
    conn = sqlite3.connect(staging)
    conn.execute(
        "UPDATE agam_classifications SET privacy='PORTABLE',kind='GUIDANCE',"
        "confidence=0.99,reason_code='forged' WHERE entity_id=2"
    )
    conn.execute("UPDATE entities SET description='[PORTABLE] forged' WHERE id=2")
    conn.commit()
    conn.close()

    with pytest.raises(ClassifierContractError) as error:
        classify_graph(source, staging, _classifying_runner([]))

    assert error.value.code == "staging_tampered"


def test_classifier_emits_run_integrity_for_the_complete_publishable_snapshot(
    tmp_path,
):
    source = tmp_path / "source.db"
    staging = tmp_path / "staging.db"
    _seed_source(source)

    classify_graph(source, staging, _classifying_runner([]))

    conn = sqlite3.connect(staging)
    table_count = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name='agam_run_integrity'"
    ).fetchone()[0]
    row = (
        conn.execute(
            "SELECT schema_version,algorithm,signature FROM agam_run_integrity"
        ).fetchone()
        if table_count
        else None
    )
    conn.close()
    assert table_count == 1
    assert row is not None
    assert row[:2] == ("1", "hmac-sha256")
    assert len(row[2]) == 64
    assert set(row[2]).issubset(set("0123456789abcdef"))


@pytest.mark.parametrize(
    ("statement", "parameters"),
    [
        ("UPDATE entities SET name=? WHERE id=1", ("forged-name",)),
        ("UPDATE entities SET type=? WHERE id=1", ("forged-type",)),
        ("UPDATE properties SET value=? WHERE entity_id=1", ("forged-property",)),
        (
            "UPDATE relationships SET relation=? WHERE id=1",
            ("forged-relation",),
        ),
        (
            "UPDATE agam_classifier_meta SET value=? WHERE key='model'",
            ("forged-model",),
        ),
    ],
)
def test_resume_rejects_tampering_of_any_run_integrity_covered_field(
    tmp_path, statement, parameters
):
    source = tmp_path / "source.db"
    staging = tmp_path / "staging.db"
    _seed_source(source)
    classify_graph(source, staging, _classifying_runner([]))
    conn = sqlite3.connect(staging)
    conn.execute(statement, parameters)
    conn.commit()
    conn.close()

    with pytest.raises(ClassifierContractError) as error:
        classify_graph(source, staging, _classifying_runner([]))

    assert error.value.code == "staging_tampered"


def test_source_wal_commit_is_detected_as_drift(tmp_path):
    source = tmp_path / "source.db"
    staging = tmp_path / "staging.db"
    _seed_source(source)
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    physical_hash = sha256_file(source)

    def wal_drift(prompt):
        writer.execute("UPDATE entities SET description='WAL_DRIFT' WHERE id=1")
        writer.commit()
        return _classifying_runner([])(prompt)

    try:
        with pytest.raises(ClassifierContractError) as error:
            classify_graph(source, staging, _with_model(wal_drift))
        assert error.value.code == "source_changed"
        assert sha256_file(source) == physical_hash
    finally:
        writer.close()


def test_existing_staging_symlink_is_rejected(tmp_path):
    source = tmp_path / "source.db"
    real_staging = tmp_path / "real-staging.db"
    alias = tmp_path / "alias-staging.db"
    _seed_source(source)
    classify_graph(source, real_staging, _classifying_runner([]))
    alias.symlink_to(real_staging)

    with pytest.raises(ClassifierContractError) as error:
        classify_graph(source, alias, _classifying_runner([]))

    assert error.value.code == "invalid_staging_path"


def test_model_response_size_is_bounded_before_json_parsing():
    opaque_id = make_opaque_id(BATCH_ID, 0)
    oversized = "S" * (MAX_MODEL_RESPONSE_CHARS + 1)

    with pytest.raises(ClassifierContractError) as error:
        parse_classifier_response(oversized, [opaque_id])

    assert error.value.code == "response_too_large"
    assert oversized not in str(error.value)


class _CaptureStdin(io.BytesIO):
    def close(self):
        self.captured = self.getvalue()
        super().close()


class _FakeProcess:
    def __init__(self, stdout, returncode=0):
        self.stdin = _CaptureStdin()
        self.stdout = io.BytesIO(stdout.encode("utf-8"))
        self.returncode = returncode
        self.killed = False
        self.wait_timeout = None

    def wait(self, timeout=None):
        self.wait_timeout = timeout
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class _BlockingStdin:
    def __init__(self, killed):
        self.killed = killed

    def write(self, _data):
        self.killed.wait(timeout=0.5)
        raise BrokenPipeError

    def close(self):
        pass


class _NonReadingProcess:
    def __init__(self):
        self.killed = threading.Event()
        self.stdin = _BlockingStdin(self.killed)
        self.stdout = io.BytesIO()
        self.returncode = None

    def wait(self, timeout=None):
        if self.killed.wait(timeout=timeout):
            self.returncode = -9
            return self.returncode
        raise subprocess.TimeoutExpired("claude-test", timeout)

    def kill(self):
        self.killed.set()


def test_claude_cli_runner_uses_safe_subprocess_and_redacts_failures(
    monkeypatch, tmp_path
):
    observed = {}

    def fake_popen(argv, **kwargs):
        observed["argv"] = argv
        observed.update(kwargs)
        process = _FakeProcess('{"items":[]}')
        observed["process"] = process
        return process

    monkeypatch.setenv("AGAM_SECRET", "must-not-be-inherited")
    monkeypatch.setattr(
        "agam.knowledge_classifier.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unbounded run")),
    )
    monkeypatch.setattr("agam.knowledge_classifier.subprocess.Popen", fake_popen)
    runner = ClaudeCliRunner(
        executable="claude-test",
        model="haiku",
        timeout=17,
        working_directory=tmp_path,
    )

    assert runner("SYNTHETIC_PROMPT") == '{"items":[]}'
    assert observed["argv"][0] == "claude-test"
    assert ["--model", "haiku"] == observed["argv"][
        observed["argv"].index("--model") : observed["argv"].index("--model") + 2
    ]
    for flag in (
        "--safe-mode",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--strict-mcp-config",
        "--tools",
        "--max-turns",
        "--no-chrome",
        "--setting-sources",
        "--system-prompt",
    ):
        assert flag in observed["argv"]
    assert observed["argv"][observed["argv"].index("--tools") + 1] == ""
    assert observed["process"].stdin.captured == b"SYNTHETIC_PROMPT"
    assert observed["stdin"] is subprocess.PIPE
    assert observed["stdout"] is subprocess.PIPE
    assert observed["stderr"] is subprocess.DEVNULL
    assert 16 < observed["process"].wait_timeout <= 17
    assert observed["shell"] is False
    assert observed["cwd"] == str(tmp_path)
    assert "AGAM_SECRET" not in observed["env"]
    assert observed["env"]["CLAUDE_CODE_SKIP_PROMPT_HISTORY"] == "1"
    assert observed["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"

    monkeypatch.setattr(
        "agam.knowledge_classifier.subprocess.Popen",
        lambda *_args, **_kwargs: _FakeProcess("RAW_OUTPUT", returncode=1),
    )
    with pytest.raises(ClassifierContractError) as error:
        runner("RAW_PROMPT")
    assert error.value.code == "runner_failed"
    assert "RAW" not in str(error.value)


def test_claude_cli_runner_rejects_oversized_stdout(monkeypatch, tmp_path):
    process = _FakeProcess("X" * (MAX_MODEL_RESPONSE_CHARS + 1))
    monkeypatch.setattr(
        "agam.knowledge_classifier.subprocess.Popen", lambda *args, **kwargs: process
    )
    runner = ClaudeCliRunner(working_directory=tmp_path)

    with pytest.raises(ClassifierContractError) as error:
        runner("synthetic")

    assert error.value.code == "response_too_large"
    assert process.killed is True


def test_claude_cli_timeout_includes_blocked_stdin_write(monkeypatch, tmp_path):
    process = _NonReadingProcess()
    monkeypatch.setattr(
        "agam.knowledge_classifier.subprocess.Popen", lambda *args, **kwargs: process
    )
    runner = ClaudeCliRunner(working_directory=tmp_path, timeout=0.05)

    started = time.monotonic()
    with pytest.raises(ClassifierContractError) as error:
        runner("X" * 1_000_000)

    assert error.value.code == "runner_failed"
    assert time.monotonic() - started < 0.3
    assert process.killed.is_set()


def test_classify_graph_rejects_runner_model_mismatch(tmp_path):
    source = tmp_path / "source.db"
    staging = tmp_path / "staging.db"
    _seed_source(source)
    runner = ClaudeCliRunner(model="sonnet", working_directory=tmp_path)

    with pytest.raises(ClassifierContractError) as error:
        classify_graph(source, staging, runner, model="haiku")

    assert error.value.code == "invalid_run_config"


def test_classify_graph_rejects_runner_without_model_identity(tmp_path):
    source = tmp_path / "source.db"
    staging = tmp_path / "staging.db"
    _seed_source(source)

    with pytest.raises(ClassifierContractError) as error:
        classify_graph(source, staging, lambda _prompt: '{"items":[]}')

    assert error.value.code == "invalid_run_config"
