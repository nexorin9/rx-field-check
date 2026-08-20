"""`rxchk.parsers.prescription` 的单元测试。

覆盖：
- 正常路径：dict / JSON 字符串 / 文件路径 三种入参形态
- 异常路径：缺字段 / 非法 JSON / 类型错 / items[] 为空
- inbox 投递：`watch_inbox` 解析成功 → processed/；解析失败 → failed/；回调失败 → failed/
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rxchk.models import Prescription, PrescriptionItem
from rxchk.parsers import PrescriptionParseError, parse_prescription, watch_inbox


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


VALID_RX_DICT: dict = {
    "prescription_no": "RX-2026-<EXAMPLE>",
    "patient_id": "<EXAMPLE_PATIENT>",
    "visit_no": "<EXAMPLE_VISIT>",
    "doctor_id": "<EXAMPLE_DOCTOR>",
    "items": [
        {
            "drug_code": "<EXAMPLE_DRUG_A>",
            "dose": "0.5g",
            "frequency": "bid",
            "route": "po",
            "duration_days": 7,
        }
    ],
    "diagnoses": [{"code": "<EXAMPLE_ICD>", "name": "示例诊断"}],
    "allergies": ["<EXAMPLE_ALLERGEN>"],
}

VALID_RX_JSON: str = json.dumps(VALID_RX_DICT, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 正常路径                                                                    #
# --------------------------------------------------------------------------- #


def test_parse_from_dict_returns_prescription() -> None:
    rx = parse_prescription(VALID_RX_DICT)
    assert isinstance(rx, Prescription)
    assert rx.prescription_no == "RX-2026-<EXAMPLE>"
    assert rx.patient_id == "<EXAMPLE_PATIENT>"
    assert rx.visit_no == "<EXAMPLE_VISIT>"
    assert rx.doctor_id == "<EXAMPLE_DOCTOR>"
    assert len(rx.items) == 1
    assert isinstance(rx.items[0], PrescriptionItem)
    assert rx.items[0].drug_code == "<EXAMPLE_DRUG_A>"
    assert rx.items[0].dose == "0.5g"
    assert rx.items[0].frequency == "bid"
    assert rx.items[0].route == "po"
    assert rx.items[0].duration_days == 7
    assert len(rx.diagnoses) == 1
    assert rx.diagnoses[0].code == "<EXAMPLE_ICD>"
    assert rx.allergies == ["<EXAMPLE_ALLERGEN>"]


def test_parse_from_json_string() -> None:
    rx = parse_prescription(VALID_RX_JSON)
    assert isinstance(rx, Prescription)
    assert rx.prescription_no == "RX-2026-<EXAMPLE>"
    assert len(rx.items) == 1


def test_parse_from_json_string_with_extra_whitespace() -> None:
    padded = "  \n" + VALID_RX_JSON + "\n  "
    rx = parse_prescription(padded)
    assert rx.prescription_no == "RX-2026-<EXAMPLE>"


def test_parse_from_json_file(tmp_path: Path) -> None:
    rx_path = tmp_path / "RX-2026-<EXAMPLE>.json"
    rx_path.write_text(VALID_RX_JSON, encoding="utf-8")

    rx = parse_prescription(rx_path)
    assert isinstance(rx, Prescription)
    assert rx.prescription_no == "RX-2026-<EXAMPLE>"


def test_parse_preserves_multiple_items() -> None:
    multi = {
        **VALID_RX_DICT,
        "items": [
            {
                "drug_code": f"<EXAMPLE_DRUG_{c}>",
                "dose": "0.5g",
                "frequency": "bid",
                "route": "po",
                "duration_days": 7,
            }
            for c in "ABC"
        ],
    }
    rx = parse_prescription(multi)
    assert len(rx.items) == 3
    assert [it.drug_code for it in rx.items] == [
        "<EXAMPLE_DRUG_A>",
        "<EXAMPLE_DRUG_B>",
        "<EXAMPLE_DRUG_C>",
    ]


def test_parse_optional_fields_default_to_empty() -> None:
    minimal = {
        "prescription_no": "RX-2026-<EXAMPLE>",
        "patient_id": "<EXAMPLE_PATIENT>",
        "visit_no": "<EXAMPLE_VISIT>",
        "doctor_id": "<EXAMPLE_DOCTOR>",
        "items": [
            {
                "drug_code": "<EXAMPLE_DRUG_A>",
                "dose": "0.5g",
                "frequency": "qd",
                "route": "po",
            }
        ],
    }
    rx = parse_prescription(minimal)
    assert rx.diagnoses == []
    assert rx.allergies == []
    assert rx.items[0].duration_days is None


# --------------------------------------------------------------------------- #
# 异常路径                                                                    #
# --------------------------------------------------------------------------- #


def test_parse_invalid_json_string_raises() -> None:
    with pytest.raises(PrescriptionParseError) as excinfo:
        parse_prescription("{not valid json")
    msg = str(excinfo.value)
    assert "invalid JSON" in msg


def test_parse_json_root_not_object_raises() -> None:
    with pytest.raises(PrescriptionParseError) as excinfo:
        parse_prescription("[1, 2, 3]")
    assert "root must be object" in str(excinfo.value)


def test_parse_unsupported_input_type_raises() -> None:
    with pytest.raises(PrescriptionParseError) as excinfo:
        parse_prescription(12345)  # type: ignore[arg-type]
    assert "unsupported input type" in str(excinfo.value)


def test_parse_missing_top_field_raises() -> None:
    bad = {k: v for k, v in VALID_RX_DICT.items() if k != "patient_id"}
    with pytest.raises(PrescriptionParseError) as excinfo:
        parse_prescription(bad)
    assert "patient_id" in str(excinfo.value)
    assert "missing required fields" in str(excinfo.value)


def test_parse_missing_multiple_top_fields_raises() -> None:
    bad = {"items": VALID_RX_DICT["items"]}  # 只剩 items
    with pytest.raises(PrescriptionParseError) as excinfo:
        parse_prescription(bad)
    msg = str(excinfo.value)
    for key in ("prescription_no", "patient_id", "visit_no", "doctor_id"):
        assert key in msg


def test_parse_empty_items_raises() -> None:
    bad = {**VALID_RX_DICT, "items": []}
    with pytest.raises(PrescriptionParseError) as excinfo:
        parse_prescription(bad)
    assert "items must not be empty" in str(excinfo.value)


def test_parse_items_not_list_raises() -> None:
    bad = {**VALID_RX_DICT, "items": "not a list"}
    with pytest.raises(PrescriptionParseError) as excinfo:
        parse_prescription(bad)
    assert "items must be a list" in str(excinfo.value)


def test_parse_item_not_object_raises() -> None:
    bad = {**VALID_RX_DICT, "items": ["not an object"]}
    with pytest.raises(PrescriptionParseError) as excinfo:
        parse_prescription(bad)
    assert "items[0]" in str(excinfo.value)


def test_parse_missing_item_field_raises() -> None:
    bad_item = {
        **VALID_RX_DICT,
        "items": [{"drug_code": "<EXAMPLE_DRUG_A>", "dose": "0.5g"}],
    }
    with pytest.raises(PrescriptionParseError) as excinfo:
        parse_prescription(bad_item)
    msg = str(excinfo.value)
    assert "items[0]" in msg
    for key in ("frequency", "route"):
        assert key in msg


def test_parse_negative_duration_days_raises() -> None:
    bad = {
        **VALID_RX_DICT,
        "items": [
            {**VALID_RX_DICT["items"][0], "duration_days": -3}
        ],
    }
    with pytest.raises(PrescriptionParseError) as excinfo:
        parse_prescription(bad)
    assert "duration_days" in str(excinfo.value)


def test_parse_diagnoses_wrong_type_raises() -> None:
    bad = {**VALID_RX_DICT, "diagnoses": "should be list"}
    with pytest.raises(PrescriptionParseError) as excinfo:
        parse_prescription(bad)
    assert "diagnoses must be a list" in str(excinfo.value)


def test_parse_diagnosis_missing_code_raises() -> None:
    bad = {**VALID_RX_DICT, "diagnoses": [{"name": "示例"}]}
    with pytest.raises(PrescriptionParseError) as excinfo:
        parse_prescription(bad)
    assert "diagnoses[0]" in str(excinfo.value)


def test_parse_allergies_wrong_type_raises() -> None:
    bad = {**VALID_RX_DICT, "allergies": "<EXAMPLE_ALLERGEN>"}
    with pytest.raises(PrescriptionParseError) as excinfo:
        parse_prescription(bad)
    assert "allergies must be a list" in str(excinfo.value)


def test_parse_allergy_wrong_element_type_raises() -> None:
    bad = {**VALID_RX_DICT, "allergies": [123]}
    with pytest.raises(PrescriptionParseError) as excinfo:
        parse_prescription(bad)
    assert "allergies[0]" in str(excinfo.value)


def test_parse_blank_prescription_no_raises() -> None:
    bad = {**VALID_RX_DICT, "prescription_no": "  "}
    with pytest.raises(PrescriptionParseError) as excinfo:
        parse_prescription(bad)
    assert "prescription_no" in str(excinfo.value)


def test_parse_file_not_found_raises(tmp_path: Path) -> None:
    """`parse_prescription(<不存在的文件>)`：文件读不到 → 解析失败。"""
    ghost = tmp_path / "nope.json"
    with pytest.raises(PrescriptionParseError) as excinfo:
        parse_prescription(ghost)
    assert "cannot read" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# watch_inbox 文件投递模式                                                    #
# --------------------------------------------------------------------------- #


def _write_rx(path: Path, payload: dict | str) -> Path:
    """helper：写入 JSON 到 path（接受 dict 或 str）。"""
    if isinstance(payload, dict):
        text = json.dumps(payload, ensure_ascii=False)
    else:
        text = payload
    path.write_text(text, encoding="utf-8")
    return path


def test_watch_inbox_processes_valid_files(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _write_rx(inbox / "rx_a.json", VALID_RX_DICT)
    _write_rx(inbox / "rx_b.json", VALID_RX_DICT)

    received: list[tuple[str, Path]] = []

    def on_rx(rx: Prescription, src: Path) -> None:
        received.append((rx.prescription_no, src))

    handled = watch_inbox(inbox, on_rx)

    assert len(handled) == 2
    assert sorted(p.name for p in handled) == ["rx_a.json", "rx_b.json"]
    assert [r[0] for r in received] == [
        "RX-2026-<EXAMPLE>",
        "RX-2026-<EXAMPLE>",
    ]
    # processed/ 子目录存在且包含这两个文件
    processed = inbox / "processed"
    assert processed.is_dir()
    assert {p.name for p in processed.iterdir()} == {"rx_a.json", "rx_b.json"}
    # inbox 下不再有原始文件（仅剩 watch_inbox 自动创建的 processed / failed 子目录）
    assert {p.name for p in inbox.iterdir()} == {"processed", "failed"}


def test_watch_inbox_moves_invalid_to_failed(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _write_rx(inbox / "good.json", VALID_RX_DICT)
    _write_rx(inbox / "broken.json", "{not json")
    _write_rx(inbox / "missing_field.json", {"items": VALID_RX_DICT["items"]})

    received: list[Path] = []

    def on_rx(rx: Prescription, src: Path) -> None:
        received.append(src)

    handled = watch_inbox(inbox, on_rx)

    # 只有 good.json 走通；broken + missing_field 移到 failed/
    assert len(handled) == 1
    assert handled[0].name == "good.json"
    failed = inbox / "failed"
    assert failed.is_dir()
    failed_names = {p.name for p in failed.iterdir()}
    assert {"broken.json", "missing_field.json"} <= failed_names
    # 错误元数据 .err 旁路文件存在
    assert (failed / "broken.json.err").is_file()
    assert (failed / "missing_field.json.err").is_file()


def test_watch_inbox_non_json_left_in_place(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _write_rx(inbox / "rx.json", VALID_RX_DICT)
    # 非 JSON 后缀 → 保留在 inbox，不动
    (inbox / "README.txt").write_text("not a json", encoding="utf-8")

    handled = watch_inbox(inbox, lambda rx, src: None)

    assert len(handled) == 1
    assert handled[0].name == "rx.json"
    assert (inbox / "README.txt").is_file()


def test_watch_inbox_callback_failure_moves_to_failed(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _write_rx(inbox / "rx.json", VALID_RX_DICT)

    def boom(rx: Prescription, src: Path) -> None:
        raise RuntimeError("downstream writeback failed")

    handled = watch_inbox(inbox, boom)
    # 回调失败 → 整张处方算失败，移到 failed/
    assert handled == []
    failed = inbox / "failed"
    assert (failed / "rx.json").is_file()
    assert "RuntimeError" in (failed / "rx.json.err").read_text(encoding="utf-8")


def test_watch_inbox_invalid_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(PrescriptionParseError) as excinfo:
        watch_inbox(tmp_path / "not_a_dir", lambda rx, src: None)
    assert "not a directory" in str(excinfo.value)


def test_watch_inbox_empty_inbox(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    assert watch_inbox(inbox, lambda rx, src: None) == []
    # processed/ 与 failed/ 已创建
    assert (inbox / "processed").is_dir()
    assert (inbox / "failed").is_dir()


def test_watch_inbox_deterministic_order(tmp_path: Path) -> None:
    """文件按文件名升序处理，便于复跑 / 调试。"""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _write_rx(inbox / "rx_03.json", VALID_RX_DICT)
    _write_rx(inbox / "rx_01.json", VALID_RX_DICT)
    _write_rx(inbox / "rx_02.json", VALID_RX_DICT)

    seen: list[str] = []

    def on_rx(rx: Prescription, src: Path) -> None:
        seen.append(src.name)

    watch_inbox(inbox, on_rx)
    assert seen == ["rx_01.json", "rx_02.json", "rx_03.json"]