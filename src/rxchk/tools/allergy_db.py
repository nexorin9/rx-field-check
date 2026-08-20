"""过敏库适配器：抽象接口 + YAML 替身实现。

设计目标
--------
- **抽象 Protocol 是正式对接契约**：HIS / 院内过敏库 / 第三方过敏字典
  通过同一组方法接入；本轮替身只读 YAML。
- **家族交叉（如青霉素 → 头孢类）**：临床过敏冲突的高发原因；通过
  `cross_reactions` 字段声明同族关系，命中时按家族交叉严重度升级。
- **严重度推断**：mild / moderate / severe / contraindicated 四档，
  Viewer 字段证据卡按严重度配色。

YAML 结构（`data/allergy_db.yaml`）：
    version: v2026.08
    allergens:
      - allergen_id: A-PENICILLIN
        names: [penicillin, amoxicillin, ampillicin]
        family: beta_lactam
        severity: severe
        cross_reactions:
          - cephalosporin  # 同族触发「家族交叉」提示
        clinical_note: 青霉素过敏者使用头孢类需皮试或换药

源 repo 名 / slug 不出现在产品代码或字符串字面值。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol, runtime_checkable

import yaml

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 自定义异常 / 枚举                                                            #
# --------------------------------------------------------------------------- #


class AllergyDbError(RuntimeError):
    """过敏库适配器调用 / 配置错误。"""


class AllergySeverity(str, Enum):
    """过敏冲突严重度。"""

    MILD = "mild"
    MODERATE = "moderate"
    SEVERE = "severe"
    CONTRAINDICATED = "contraindicated"


# 严重度 → Viewer 字段卡 severity 字段映射。
SEVERITY_TO_FINDING: dict[AllergySeverity, str] = {
    AllergySeverity.MILD: "low",
    AllergySeverity.MODERATE: "medium",
    AllergySeverity.SEVERE: "high",
    AllergySeverity.CONTRAINDICATED: "high",
}


# --------------------------------------------------------------------------- #
# 数据结构                                                                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AllergenEntry:
    """单条过敏原条目。"""

    allergen_id: str
    names: tuple[str, ...]
    family: str
    severity: AllergySeverity
    cross_reactions: tuple[str, ...]
    clinical_note: str


@dataclass(frozen=True)
class AllergyHit:
    """单条过敏冲突命中。"""

    allergen: str
    drug_code: str
    severity: AllergySeverity
    cross_reactivity: bool
    clinical_note: str
    evidence: str
    rule_id: str


# --------------------------------------------------------------------------- #
# 抽象接口                                                                    #
# --------------------------------------------------------------------------- #


@runtime_checkable
class AllergyDbProtocol(Protocol):
    """过敏库适配器抽象接口。

    5 个核心方法：
    - `get_db_version`：规则库版本号（与 YAML `version` 字段一致），便于跨版本审计
    - `check`：单药 × 多过敏原 批量命中查询（直接命中 + 家族交叉合一返回）
    - `family_cross_reactivity`：单药 × 单过敏原 显式家族交叉探测
      （用于「患者对青霉素过敏 → 处方头孢」的判定）
    - `severity_for`：按过敏原名反查 finding severity（直接命中视角）
    - `severity_inference`：综合 `(drug_code, allergies)` 推断最高严重度档
      （含直接命中 + 家族交叉降档后），返回 finding severity（high/medium/low/info）
    """

    def get_db_version(self) -> str: ...

    def check(
        self, drug_code: str, allergies: Iterable[str]
    ) -> list[AllergyHit]: ...

    def family_cross_reactivity(
        self, drug_code: str, allergy: str
    ) -> Optional[AllergyHit]: ...

    def severity_for(self, allergen: str) -> Optional[str]: ...

    def severity_inference(
        self, drug_code: str, allergies: Iterable[str]
    ) -> Optional[str]: ...


# --------------------------------------------------------------------------- #
# 替身实现                                                                    #
# --------------------------------------------------------------------------- #


class FakeAllergyDb:
    """过敏库替身：从 YAML 文件读取过敏原条目。

    行为约定：
    - 直接命中：处方药品名 / 编码出现在过敏原 `names` 列表中
    - 家族交叉：处方药品名 / 编码命中某过敏原 `cross_reactions` 中声明
      的同族 → 命中但 `cross_reactivity=true`
    - 家族匹配升级：直接命中 severe / contraindicated → 升级为最高档；
      家族交叉降一档（severe → major 视角），由 Viewer 配色提示药师注意
    """

    def __init__(
        self,
        allergy_db_path: str | Path,
        drug_dict: Any = None,
    ) -> None:
        self._path = Path(allergy_db_path)
        self._drug_dict = drug_dict  # 可选：用于按 drug_code 找药名
        self._entries: dict[str, AllergenEntry] = {}
        self._name_to_entry: dict[str, AllergenEntry] = {}
        self._family_to_entries: dict[str, list[AllergenEntry]] = {}
        self._version: str = "unknown"
        self._load()
        logger.info(
            "[FAKE] FakeAllergyDb loaded: %s (%d allergens, version=%s)",
            self._path.name,
            len(self._entries),
            self._version,
        )

    def _load(self) -> None:
        if not self._path.exists():
            raise AllergyDbError(f"allergy db file not found: {self._path}")
        try:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise AllergyDbError(f"invalid YAML in {self._path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise AllergyDbError(f"allergy db root must be mapping: {self._path}")
        self._version = str(raw.get("version", "unknown"))
        for entry in raw.get("allergens", []) or []:
            parsed = _parse_allergen_entry(entry)
            self._entries[parsed.allergen_id] = parsed
            for name in parsed.names:
                self._name_to_entry[name.lower()] = parsed
            self._family_to_entries.setdefault(parsed.family, []).append(parsed)

    # ------------------------------------------------------------------ #
    # 内部：解析时用                                                        #
    # ------------------------------------------------------------------ #

    def _match_allergen_for_drug(
        self, drug_code: str, allergy: str
    ) -> Optional[tuple[AllergenEntry, bool]]:
        """返回 (entry, cross_reactivity)。

        优先级：直接命中 > 家族交叉；命中时携带是否家族交叉。

        直接命中语义：患者对 `allergy` 过敏 → 处方药品是该过敏原
          （`allergy_db.yaml` 中某 entry 的 `names` 之一）。

        家族交叉语义：患者对 `allergy` 过敏 → 处方药品属于
          `allergy` 的 `cross_reactions` 列出的同族 → 通过
          `_drug_matches(drug_code, cr)` 命中。这里 `cr` 是
          同族名（如 `cephalosporin`）；另一 entry（如
          A-CEPHALOSPORIN）的 `names` 含有 `cephalosporin` 且
          也含有该药品编码（如 `DRUG_CEPH`），即可命中。
        """
        allergy_lower = allergy.strip().lower()
        # 1. 过敏原直接命中：患者过敏名 == entry 名，药品在 entry.names 中
        direct_entry = self._name_to_entry.get(allergy_lower)
        if direct_entry is not None:
            for name in direct_entry.names:
                if self._drug_matches(drug_code, name):
                    return direct_entry, False

        # 2. 家族交叉：处方药品属于某 entry 的 names，且该 entry 的
        #    cross_reactions 含有 `allergy_lower`（反向交叉关系成立）
        for entry in self._entries.values():
            drug_in_entry = False
            for name in entry.names:
                if self._drug_matches(drug_code, name):
                    drug_in_entry = True
                    break
            if not drug_in_entry:
                continue
            if any(cr.lower() == allergy_lower for cr in entry.cross_reactions):
                # 命中家族交叉：source_entry 是患者过敏的 entry（用来
                # 携带过敏严重度）；cross=True 由调用方上色。
                source = direct_entry or entry
                return source, True

        # 3. 反向：扫描所有 entry，看是否有 entry 的 cross_reactions 命中 allergy_lower
        #    且药品直接 match 该 entry 的 name（兜底，避免漏掉）
        for entry in self._entries.values():
            if any(cr.lower() == allergy_lower for cr in entry.cross_reactions):
                for name in entry.names:
                    if self._drug_matches(drug_code, name):
                        source = direct_entry or entry
                        return source, True

        return None

    def _drug_matches(self, drug_code: str, allergen_name: str) -> bool:
        """判断药品是否匹配某过敏原名。

        优先级：
        1. drug_code 自身与 allergen_name 全等（大小写不敏感）
        2. drug_dict 查到的药品名与 allergen_name 全等
        """
        if not drug_code or not allergen_name:
            return False
        if drug_code.strip().lower() == allergen_name.strip().lower():
            return True
        if self._drug_dict is not None:
            try:
                rec = self._drug_dict.lookup_drug(drug_code)
            except Exception:  # noqa: BLE001 — drug_dict 异常不影响主流程
                rec = None
            if rec is not None and rec.name.lower() == allergen_name.lower():
                return True
        return False

    # ------------------------------------------------------------------ #
    # Protocol 方法                                                          #
    # ------------------------------------------------------------------ #

    def get_db_version(self) -> str:
        return self._version

    def check(
        self, drug_code: str, allergies: Iterable[str]
    ) -> list[AllergyHit]:
        hits: list[AllergyHit] = []
        for allergy in allergies:
            match = self._match_allergen_for_drug(drug_code, allergy)
            if match is None:
                continue
            entry, cross = match
            # 家族交叉降一档
            sev = entry.severity
            if cross and sev is AllergySeverity.SEVERE:
                sev = AllergySeverity.MODERATE
            elif cross and sev is AllergySeverity.CONTRAINDICATED:
                sev = AllergySeverity.SEVERE
            evidence = (
                f"<ALLERGY_DB:{entry.allergen_id}/{drug_code}/"
                f"{'cross' if cross else 'direct'}>"
            )
            rule_id = (
                f"R-ALG-{entry.allergen_id}-"
                f"{'CROSS' if cross else 'DIRECT'}"
            )
            note = entry.clinical_note
            if cross:
                note = f"家族交叉：{entry.clinical_note}"
            hits.append(
                AllergyHit(
                    allergen=allergy,
                    drug_code=drug_code,
                    severity=sev,
                    cross_reactivity=cross,
                    clinical_note=note,
                    evidence=evidence,
                    rule_id=rule_id,
                )
            )
        return hits

    def family_cross_reactivity(
        self, drug_code: str, allergy: str
    ) -> Optional[AllergyHit]:
        hits = self.check(drug_code, [allergy])
        for h in hits:
            if h.cross_reactivity:
                return h
        return None

    def severity_for(self, allergen: str) -> Optional[str]:
        entry = self._name_to_entry.get(allergen.strip().lower())
        if entry is None:
            return None
        return SEVERITY_TO_FINDING[entry.severity]

    def severity_inference(
        self, drug_code: str, allergies: Iterable[str]
    ) -> Optional[str]:
        """综合推断 (drug_code, allergies) 最高严重度档。

        用途：
        - 不需要看具体命中细节时（前置审方快速筛 / 简单规则引擎），
          一次调用拿到「最高冲突严重度」
        - 同时考虑直接命中 + 家族交叉（家族交叉已自动降档）

        返回：finding severity 字符串（`high` / `medium` / `low` / `info`），
        若全部未命中 → None。

        注意：当 `allergies` 为空 / 仅含未登记过敏原 → 返回 None，
        不伪命中。
        """
        hits = self.check(drug_code, allergies)
        if not hits:
            return None
        # 严重度排序（轻 → 重）
        order = (
            AllergySeverity.MILD,
            AllergySeverity.MODERATE,
            AllergySeverity.SEVERE,
            AllergySeverity.CONTRAINDICATED,
        )
        top = max(hits, key=lambda h: order.index(h.severity))
        return SEVERITY_TO_FINDING[top.severity]


# --------------------------------------------------------------------------- #
# 工具函数                                                                    #
# --------------------------------------------------------------------------- #


def _parse_allergen_entry(entry: Any) -> AllergenEntry:
    if not isinstance(entry, dict):
        raise AllergyDbError(
            f"allergen entry must be mapping, got {type(entry).__name__}"
        )
    try:
        allergen_id = str(entry["allergen_id"])
        family = str(entry["family"])
    except KeyError as exc:
        raise AllergyDbError(
            f"allergen entry missing required field: {exc}"
        ) from exc
    names = tuple(str(n) for n in entry.get("names", []) or ())
    if not names:
        raise AllergyDbError(
            f"allergen {allergen_id} must have at least one name"
        )
    cross = tuple(str(c) for c in entry.get("cross_reactions", []) or ())
    try:
        severity = AllergySeverity(str(entry.get("severity", "moderate")).lower())
    except ValueError:
        severity = AllergySeverity.MODERATE
    note = str(entry.get("clinical_note") or "过敏冲突，请人工复核")
    return AllergenEntry(
        allergen_id=allergen_id,
        names=names,
        family=family,
        severity=severity,
        cross_reactions=cross,
        clinical_note=note,
    )


__all__ = [
    "AllergyDbError",
    "AllergyDbProtocol",
    "AllergyHit",
    "AllergySeverity",
    "AllergenEntry",
    "FakeAllergyDb",
    "SEVERITY_TO_FINDING",
]