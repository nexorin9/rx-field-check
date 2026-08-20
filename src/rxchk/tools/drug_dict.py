"""药品字典适配器：抽象接口 + YAML 替身实现。

设计目标
--------
- **抽象 Protocol 是正式对接契约**：HIS / 院内药品字典 / 第三方编码库
  通过同一组方法接入；本轮替身只读 YAML。
- **替身实现显式标注 `[FAKE]`**：与规则引擎替身一致，便于审计员识别。
- **dose / frequency / route 由药品字典主数据决定**：避免规则适配器重复
  维护（如「某药最大日剂量」由药品字典给出，「是否超上限」由规则适配器
  判定，二者解耦）。

YAML 结构（`data/drug_dict.yaml`）：
    version: v2026.08
    drugs:
      - code: DRUG_A
        name: 阿莫西林胶囊
        forms: [capsule, tablet]
        routes: [po]
        default_frequency: tid
        max_single_dose_g: 0.5
        max_daily_dose_g: 2.0
        per_route_limits:
          po: {single_g: 0.5, daily_g: 2.0}
          iv: {single_g: 1.0, daily_g: 4.0}
        indications: [J18.900, J20.900]

源 repo 名 / slug 不出现在产品代码或字符串字面值。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

import yaml

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 自定义异常                                                                  #
# --------------------------------------------------------------------------- #


class DrugDictError(RuntimeError):
    """药品字典适配器调用 / 配置错误。"""


# --------------------------------------------------------------------------- #
# 数据结构                                                                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DrugRecord:
    """单条药品字典记录。

    字段说明见类注释；与 YAML `drugs:` 列表元素一一对应。
    """

    code: str
    name: str
    forms: tuple[str, ...]
    routes: tuple[str, ...]
    default_frequency: str
    max_single_dose_g: Optional[float]
    max_daily_dose_g: Optional[float]
    per_route_limits: dict[str, dict[str, float]] = field(default_factory=dict)
    indications: tuple[str, ...] = ()

    def route_allowed(self, route: str) -> bool:
        return route in self.routes

    def route_limit(self, route: str) -> Optional[dict[str, float]]:
        return self.per_route_limits.get(route)


# --------------------------------------------------------------------------- #
# 抽象接口                                                                    #
# --------------------------------------------------------------------------- #


@runtime_checkable
class DrugDictProtocol(Protocol):
    """药品字典适配器抽象接口。

    本轮（任务 5）暴露查找 / 编码 / 上下限 6 个方法；任务 28 进一步
    扩展为 5 个核心方法（含 frequency_default / route_form）。
    """

    def get_dict_version(self) -> str: ...

    def lookup_drug(self, drug_code: str) -> Optional[DrugRecord]: ...

    def check_name(self, drug_code: str) -> bool: ...

    def check_encode(self, drug_code: str) -> bool: ...

    def max_single_dose(self, drug_code: str, route: str) -> Optional[float]: ...

    def max_daily_dose(self, drug_code: str, route: str) -> Optional[float]: ...

    def frequency_default(self, drug_code: str) -> Optional[str]: ...

    def route_form_match(self, drug_code: str, route: str) -> bool: ...

    def indications_for(self, drug_code: str) -> tuple[str, ...]: ...


# --------------------------------------------------------------------------- #
# 替身实现                                                                    #
# --------------------------------------------------------------------------- #


class FakeDrugDict:
    """药品字典替身：从 YAML 文件读取药品主数据。

    行为约定：
    - 找不到 drug_code → `lookup_drug` 返回 None（不抛错，Agent 走 fallback）
    - 编码检查：编码只含 [A-Z0-9_-]，长度 ≥ 3；否则返回 False
    - 途径上下限：优先 `per_route_limits[route]`；缺失回退到药品级上下限
    """

    def __init__(self, drug_dict_path: str | Path) -> None:
        self._path = Path(drug_dict_path)
        self._drugs: dict[str, DrugRecord] = {}
        self._version: str = "unknown"
        self._load()
        logger.info(
            "[FAKE] FakeDrugDict loaded: %s (%d drugs, version=%s)",
            self._path.name,
            len(self._drugs),
            self._version,
        )

    def _load(self) -> None:
        if not self._path.exists():
            raise DrugDictError(f"drug dict file not found: {self._path}")
        try:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise DrugDictError(f"invalid YAML in {self._path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise DrugDictError(f"drug dict root must be mapping: {self._path}")
        self._version = str(raw.get("version", "unknown"))
        for entry in raw.get("drugs", []) or []:
            rec = _parse_drug_entry(entry)
            self._drugs[rec.code] = rec

    # ------------------------------------------------------------------ #
    # Protocol 方法                                                          #
    # ------------------------------------------------------------------ #

    def get_dict_version(self) -> str:
        return self._version

    def lookup_drug(self, drug_code: str) -> Optional[DrugRecord]:
        return self._drugs.get(drug_code)

    def check_name(self, drug_code: str) -> bool:
        return self.lookup_drug(drug_code) is not None

    def check_encode(self, drug_code: str) -> bool:
        if not drug_code or not isinstance(drug_code, str):
            return False
        # 院内药品编码：必须是大写字母数字 + 下划线 / 横线，长度 ≥ 3
        if len(drug_code) < 3:
            return False
        return all(
            c.isupper() or c.isdigit() or c in "_-"
            for c in drug_code
        )

    def max_single_dose(self, drug_code: str, route: str) -> Optional[float]:
        rec = self.lookup_drug(drug_code)
        if rec is None:
            return None
        limit = rec.route_limit(route)
        if limit and "single_g" in limit:
            return float(limit["single_g"])
        return rec.max_single_dose_g

    def max_daily_dose(self, drug_code: str, route: str) -> Optional[float]:
        rec = self.lookup_drug(drug_code)
        if rec is None:
            return None
        limit = rec.route_limit(route)
        if limit and "daily_g" in limit:
            return float(limit["daily_g"])
        return rec.max_daily_dose_g

    def frequency_default(self, drug_code: str) -> Optional[str]:
        rec = self.lookup_drug(drug_code)
        if rec is None:
            return None
        return rec.default_frequency or None

    def route_form_match(self, drug_code: str, route: str) -> bool:
        rec = self.lookup_drug(drug_code)
        if rec is None:
            return False
        return rec.route_allowed(route)

    def indications_for(self, drug_code: str) -> tuple[str, ...]:
        rec = self.lookup_drug(drug_code)
        if rec is None:
            return ()
        return rec.indications


# --------------------------------------------------------------------------- #
# 工具函数                                                                    #
# --------------------------------------------------------------------------- #


def _parse_drug_entry(entry: Any) -> DrugRecord:
    """YAML dict → `DrugRecord`；缺字段抛 `DrugDictError`。"""
    if not isinstance(entry, dict):
        raise DrugDictError(f"drug entry must be mapping, got {type(entry).__name__}")
    try:
        code = str(entry["code"])
        name = str(entry["name"])
    except KeyError as exc:
        raise DrugDictError(f"drug entry missing required field: {exc}") from exc
    routes = tuple(str(r) for r in entry.get("routes", []) or [])
    forms = tuple(str(f) for f in entry.get("forms", []) or [])
    indications = tuple(str(i) for i in entry.get("indications", []) or [])
    per_route_limits: dict[str, dict[str, float]] = {}
    for route_key, limits in (entry.get("per_route_limits") or {}).items():
        if not isinstance(limits, dict):
            continue
        per_route_limits[str(route_key)] = {
            k: float(v) for k, v in limits.items() if isinstance(v, (int, float))
        }
    return DrugRecord(
        code=code,
        name=name,
        forms=forms,
        routes=routes,
        default_frequency=str(entry.get("default_frequency", "")),
        max_single_dose_g=_maybe_float(entry.get("max_single_dose_g")),
        max_daily_dose_g=_maybe_float(entry.get("max_daily_dose_g")),
        per_route_limits=per_route_limits,
        indications=indications,
    )


def _maybe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


__all__ = [
    "DrugDictError",
    "DrugDictProtocol",
    "DrugRecord",
    "FakeDrugDict",
]