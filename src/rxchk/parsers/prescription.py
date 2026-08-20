"""处方解析器实现。

入参形态（`parse_prescription`）：
- `dict`：已经反序列化的 JSON 对象
- `str`：JSON 字符串（医院网关透传时常见）
- 文件路径（`str` / `Path`）：HIS / 前置审方落盘的 JSON 文件

返回：`rxchk.models.Prescription` dataclass（与 spec.md 入参 schema 一一对应）。

异常：
- `PrescriptionParseError` —— 非法 JSON / 字段缺失 / 字段类型错误 / `items[]` 为空
- 不静默默认；上游必须收到错误并显式决定（拒收 / 重试 / 走 manual_override）

文件投递模式（`watch_inbox`）：
- 扫 `./inbox/<rx_no>.json` 目录，对每个未处理过的文件调 `on_rx(prescription, source_path)`
- 解析成功的文件 → 移动到 `./inbox/processed/`（兜底：失败保留 `failed/`，便于人工排查）
- 文件名非法（不以 `.json` 结尾 / 不是 JSON）→ 不动，保留在 inbox 等运维处置
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Callable, Union

from rxchk.models import (
    Diagnosis,
    Prescription,
    PrescriptionItem,
    from_dict,
)


# --------------------------------------------------------------------------- #
# 自定义异常                                                                  #
# --------------------------------------------------------------------------- #


class PrescriptionParseError(ValueError):
    """处方入参解析 / 校验失败的统一异常类型。

    上游（CLI / HTTP API / inbox watcher）据此决定：
    - HTTP API → 返回 400 + 错误结构
    - CLI → 打印错误并 exit 1
    - inbox watcher → 移到 `./inbox/failed/` 留人工排查
    """


# --------------------------------------------------------------------------- #
# 工具函数                                                                    #
# --------------------------------------------------------------------------- #


# 处方顶层必填字段；与 spec.md 入参 schema 严格对齐。
_REQUIRED_TOP_KEYS: tuple[str, ...] = (
    "prescription_no",
    "patient_id",
    "visit_no",
    "doctor_id",
    "items",
)

# 单条 item 必填字段（duration_days 可选）。
_REQUIRED_ITEM_KEYS: tuple[str, ...] = (
    "drug_code",
    "dose",
    "frequency",
    "route",
)


def _normalize(data: Any, source: str) -> dict[str, Any]:
    """把任意入参形态（dict / str JSON / Path）规范化为 dict。

    异常：非法 JSON → `PrescriptionParseError`（带 source 上下文）。
    """
    if isinstance(data, dict):
        return data
    if isinstance(data, Path):
        # Path 一律按文件读；不存在 → PrescriptionParseError
        try:
            text = data.read_text(encoding="utf-8")
        except OSError as exc:
            raise PrescriptionParseError(
                f"cannot read prescription file {data}: {exc}"
            ) from exc
        return _parse_json_text(text, source=str(data))
    if isinstance(data, str):
        # str：含路径分隔符或以 .json 结尾 → 视作文件路径；否则按 JSON 字符串解析
        if ("/" in data or "\\" in data or data.endswith(".json")) and (
            Path(data).is_file()
        ):
            try:
                text = Path(data).read_text(encoding="utf-8")
            except OSError as exc:
                raise PrescriptionParseError(
                    f"cannot read prescription file {data}: {exc}"
                ) from exc
            return _parse_json_text(text, source=data)
        return _parse_json_text(data, source="<inline json>")
    raise PrescriptionParseError(
        f"unsupported input type {type(data).__name__} (source={source!r}); "
        f"expected dict, JSON string, or path to .json file"
    )


def _parse_json_text(text: str, *, source: str) -> dict[str, Any]:
    """解析 JSON 文本 → dict；非法 JSON → `PrescriptionParseError`。"""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PrescriptionParseError(
            f"invalid JSON in {source}: line {exc.lineno} col {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(obj, dict):
        raise PrescriptionParseError(
            f"prescription JSON root must be object, got {type(obj).__name__} "
            f"(source={source!r})"
        )
    return obj


def _check_required(data: dict[str, Any]) -> None:
    """校验顶层必填字段；缺失即抛 `PrescriptionParseError`。"""
    missing = [k for k in _REQUIRED_TOP_KEYS if k not in data]
    if missing:
        raise PrescriptionParseError(
            f"prescription missing required fields: {', '.join(missing)}"
        )
    # 顶层字符串字段必须非空（空白字符也算缺失）
    for key in ("prescription_no", "patient_id", "visit_no", "doctor_id"):
        val = data.get(key)
        if not isinstance(val, str) or not val.strip():
            raise PrescriptionParseError(
                f"prescription.{key} must be a non-empty string"
            )
    if not isinstance(data["items"], list):
        raise PrescriptionParseError(
            f"prescription.items must be a list, got {type(data['items']).__name__}"
        )
    if not data["items"]:
        raise PrescriptionParseError("prescription.items must not be empty")
    for idx, item in enumerate(data["items"]):
        if not isinstance(item, dict):
            raise PrescriptionParseError(
                f"prescription.items[{idx}] must be an object, "
                f"got {type(item).__name__}"
            )
        item_missing = [k for k in _REQUIRED_ITEM_KEYS if k not in item]
        if item_missing:
            raise PrescriptionParseError(
                f"prescription.items[{idx}] missing required fields: "
                f"{', '.join(item_missing)}"
            )
        # item 字符串字段同样非空校验
        for key in _REQUIRED_ITEM_KEYS:
            val = item.get(key)
            if not isinstance(val, str) or not val.strip():
                raise PrescriptionParseError(
                    f"prescription.items[{idx}].{key} must be a non-empty string"
                )


def _coerce_diagnoses(raw: Any) -> list[dict[str, str]]:
    """规范化 diagnoses；空值兜底为空列表；元素必须是 `{code, name}`。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PrescriptionParseError(
            f"prescription.diagnoses must be a list, got {type(raw).__name__}"
        )
    out: list[dict[str, str]] = []
    for idx, d in enumerate(raw):
        if not isinstance(d, dict):
            raise PrescriptionParseError(
                f"prescription.diagnoses[{idx}] must be an object"
            )
        if "code" not in d or "name" not in d:
            raise PrescriptionParseError(
                f"prescription.diagnoses[{idx}] missing 'code' or 'name'"
            )
        out.append({"code": str(d["code"]), "name": str(d["name"])})
    return out


def _coerce_allergies(raw: Any) -> list[str]:
    """规范化 allergies；空值兜底为空列表；元素必须是字符串。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PrescriptionParseError(
            f"prescription.allergies must be a list, got {type(raw).__name__}"
        )
    out: list[str] = []
    for idx, a in enumerate(raw):
        if not isinstance(a, str):
            raise PrescriptionParseError(
                f"prescription.allergies[{idx}] must be a string, "
                f"got {type(a).__name__}"
            )
        out.append(a)
    return out


# --------------------------------------------------------------------------- #
# 公共 API                                                                    #
# --------------------------------------------------------------------------- #


# 类型别名：宽口径接受 dict / str / Path
ParseableInput = Union[dict, str, Path]


def parse_prescription(data: ParseableInput) -> Prescription:
    """解析处方入参 → `Prescription` dataclass。

    Args:
        data: dict / JSON 字符串 / JSON 文件路径。

    Returns:
        校验后的 `Prescription`（含 `items` / `diagnoses` / `allergies`）。

    Raises:
        PrescriptionParseError: 非法 JSON / 缺字段 / 字段类型错 / `items[]` 空。
    """
    source = (
        f"<{type(data).__name__}>"
        if not isinstance(data, (str, Path))
        else (str(data) if not isinstance(data, Path) else str(data))
    )
    raw = _normalize(data, source=source)
    _check_required(raw)

    # 规范化 diagnoses / allergies；缺字段视情况使用空列表
    data = dict(raw)  # 浅拷贝，避免污染调用方
    data["diagnoses"] = _coerce_diagnoses(data.get("diagnoses"))
    data["allergies"] = _coerce_allergies(data.get("allergies"))

    try:
        rx = from_dict(data)
    except (ValueError, KeyError, TypeError) as exc:
        # `from_dict` 内部 dataclass __post_init__ 抛 ValueError
        # 上层 _check_required 已覆盖 K 缺失，剩余的是类型 / 范围问题
        raise PrescriptionParseError(
            f"prescription validation failed: {exc}"
        ) from exc

    return rx


# --------------------------------------------------------------------------- #
# inbox 文件投递模式                                                          #
# --------------------------------------------------------------------------- #


# 类型：on_rx 回调签名 (prescription, source_path) -> None
OnRxCallback = Callable[[Prescription, Path], None]


def watch_inbox(
    inbox_dir: ParseableInput,
    on_rx: OnRxCallback,
    *,
    processed_dirname: str = "processed",
    failed_dirname: str = "failed",
) -> list[Path]:
    """扫 `inbox_dir` 下所有 `*.json` 文件，逐个解析并调用 `on_rx`。

    行为约定：
    - 解析成功 → 调 `on_rx(prescription, source_path)` → 把文件移动到
      `<inbox_dir>/<processed_dirname>/`（保持文件名）；若 `on_rx` 抛错，
      移到 `<inbox_dir>/<failed_dirname>/` 并**不再重试**。
    - 解析失败 → 不调 `on_rx`，直接移到 `<failed_dirname>/` 留人工排查。
    - 非 `.json` 后缀的文件 → 不动，保留在 inbox（运维处理）。
    - 子目录缺失 → 自动创建。

    Returns:
        成功处理的文件路径列表（顺序 = 文件名排序）。

    Raises:
        PrescriptionParseError: 仅当 `inbox_dir` 路径无效 / 不是目录时。
    """
    inbox = Path(inbox_dir)
    if not inbox.exists() or not inbox.is_dir():
        raise PrescriptionParseError(
            f"inbox path is not a directory: {inbox}"
        )

    processed_dir = inbox / processed_dirname
    failed_dir = inbox / failed_dirname
    processed_dir.mkdir(exist_ok=True)
    failed_dir.mkdir(exist_ok=True)

    handled: list[Path] = []

    # 列出所有非目录文件，按文件名排序（确定性顺序，便于复跑 / 调试）
    candidates = sorted(
        p for p in inbox.iterdir() if p.is_file() and p.suffix.lower() == ".json"
    )

    for src in candidates:
        try:
            rx = parse_prescription(src)
        except PrescriptionParseError as exc:
            # 解析失败：移到 failed/，不调 on_rx
            dest = failed_dir / src.name
            shutil.move(str(src), str(dest))
            # 在 failed 目录旁追加 .err 元数据，便于排查（best-effort）
            err_note = dest.with_suffix(src.suffix + ".err")
            err_note.write_text(
                f"{exc.__class__.__name__}: {exc}\n", encoding="utf-8"
            )
            continue

        # 解析成功：调回调；若回调抛错则移到 failed/
        try:
            on_rx(rx, src)
        except Exception as exc:  # noqa: BLE001 — 回调失败 = 整个投递失败
            dest = failed_dir / src.name
            shutil.move(str(src), str(dest))
            err_note = dest.with_suffix(src.suffix + ".err")
            err_note.write_text(
                f"on_rx callback raised {exc.__class__.__name__}: {exc}\n",
                encoding="utf-8",
            )
            continue

        # 成功：移到 processed/
        dest = processed_dir / src.name
        shutil.move(str(src), str(dest))
        handled.append(dest)

    return handled


__all__ = [
    "OnRxCallback",
    "ParseableInput",
    "PrescriptionParseError",
    "parse_prescription",
    "watch_inbox",
]