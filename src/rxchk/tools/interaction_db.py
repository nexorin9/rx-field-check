"""配伍库适配器：抽象接口 + YAML 替身实现。

设计目标
--------
- **抽象 Protocol 是正式对接契约**：院内配伍库 / 第三方相互作用数据库
  通过同一组方法接入；本轮替身只读 YAML。
- **严重度等级**：contraindicated / major / moderate / minor 四档，对应
  Viewer 字段证据卡的不同视觉强调。
- **双药 + 三药累加查询**：临床常见两药配伍冲突；肝代谢类三药累加风险
  单独命中。

YAML 结构（`data/interaction_db.yaml`）：
    version: v2026.08
    interactions:
      - pair: [DRUG_A, DRUG_B]
        severity: major
        clinical_note: 两药联用增加出血风险，建议监测 INR
        evidence: <INTERACTION_DB:DRUG_A+DRUG_B>
        routes_combo: null

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


class InteractionDbError(RuntimeError):
    """配伍库适配器调用 / 配置错误。"""


class InteractionSeverity(str, Enum):
    """配伍冲突严重度。

    顺序：contraindicated > major > moderate > minor。
    """

    CONTRAINDICATED = "contraindicated"
    MAJOR = "major"
    MODERATE = "moderate"
    MINOR = "minor"


# 严重度 → Viewer 字段卡 severity 字段映射（与 spec.md 4 档配色对齐）。
SEVERITY_TO_FINDING: dict[InteractionSeverity, str] = {
    InteractionSeverity.CONTRAINDICATED: "high",
    InteractionSeverity.MAJOR: "high",
    InteractionSeverity.MODERATE: "medium",
    InteractionSeverity.MINOR: "low",
}


# --------------------------------------------------------------------------- #
# 数据结构                                                                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class InteractionHit:
    """单条配伍命中。"""

    drug_a: str
    drug_b: str
    severity: InteractionSeverity
    clinical_note: str
    evidence: str
    rule_id: str


# --------------------------------------------------------------------------- #
# 抽象接口                                                                    #
# --------------------------------------------------------------------------- #


@runtime_checkable
class InteractionDbProtocol(Protocol):
    """配伍库适配器抽象接口。"""

    def get_db_version(self) -> str: ...

    def check_pair(
        self, drug_a: str, drug_b: str
    ) -> Optional[InteractionHit]: ...

    def check(self, drug_codes: Iterable[str]) -> list[InteractionHit]: ...

    def severity_level(self, drug_a: str, drug_b: str) -> Optional[str]: ...


# --------------------------------------------------------------------------- #
# 替身实现                                                                    #
# --------------------------------------------------------------------------- #


class FakeInteractionDb:
    """配伍库替身：从 YAML 文件读取相互作用数据。

    行为约定：
    - 双向命中：配置 `[A, B]` 时查 `A,B` 或 `B,A` 都返回同一命中
    - 三药累加：当配置 `triple: [A, B, C]` 时，处方含 A+B+C 即命中
    - 找不到 → `check_pair` 返回 None；`check` 返回空 list
    """

    def __init__(self, interaction_db_path: str | Path) -> None:
        self._path = Path(interaction_db_path)
        self._pair_index: dict[tuple[str, str], dict[str, Any]] = {}
        self._triples: list[dict[str, Any]] = []
        self._version: str = "unknown"
        self._load()
        logger.info(
            "[FAKE] FakeInteractionDb loaded: %s (%d pairs, %d triples, version=%s)",
            self._path.name,
            len(self._pair_index),
            len(self._triples),
            self._version,
        )

    def _load(self) -> None:
        if not self._path.exists():
            raise InteractionDbError(
                f"interaction db file not found: {self._path}"
            )
        try:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise InteractionDbError(
                f"invalid YAML in {self._path}: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise InteractionDbError(
                f"interaction db root must be mapping: {self._path}"
            )
        self._version = str(raw.get("version", "unknown"))
        for entry in raw.get("interactions", []) or []:
            self._index_pair(entry)
        for entry in raw.get("triples", []) or []:
            self._index_triple(entry)

    def _index_pair(self, entry: dict[str, Any]) -> None:
        pair = entry.get("pair") or []
        if not isinstance(pair, list) or len(pair) != 2:
            raise InteractionDbError(
                f"interaction pair must be a 2-element list: {entry}"
            )
        a, b = str(pair[0]), str(pair[1])
        # 双向索引：正序 + 倒序
        self._pair_index[(a, b)] = entry
        if (b, a) not in self._pair_index:
            self._pair_index[(b, a)] = entry

    def _index_triple(self, entry: dict[str, Any]) -> None:
        triple = entry.get("triple") or []
        if not isinstance(triple, list) or len(triple) != 3:
            raise InteractionDbError(
                f"interaction triple must be a 3-element list: {entry}"
            )
        self._triples.append(entry)

    # ------------------------------------------------------------------ #
    # Protocol 方法                                                          #
    # ------------------------------------------------------------------ #

    def get_db_version(self) -> str:
        return self._version

    def check_pair(
        self, drug_a: str, drug_b: str
    ) -> Optional[InteractionHit]:
        if drug_a == drug_b:
            return None
        entry = self._pair_index.get((drug_a, drug_b))
        if entry is None:
            return None
        return _to_hit(entry, drug_a, drug_b)

    def check(self, drug_codes: Iterable[str]) -> list[InteractionHit]:
        codes = list(drug_codes)
        # 去重保持处方顺序
        seen: set[str] = set()
        unique: list[str] = []
        for c in codes:
            if c not in seen:
                seen.add(c)
                unique.append(c)

        hits: list[InteractionHit] = []
        # 两两配伍
        for i in range(len(unique)):
            for j in range(i + 1, len(unique)):
                hit = self.check_pair(unique[i], unique[j])
                if hit is not None:
                    hits.append(hit)
        # 三药累加
        code_set = set(unique)
        for entry in self._triples:
            triple = entry.get("triple") or []
            triple_set = {str(c) for c in triple}
            if triple_set and triple_set.issubset(code_set):
                # 三药命中：用 entry 自身 + triple[0..1] 作 drug_a/drug_b
                hits.append(
                    _to_hit(
                        entry,
                        str(triple[0]),
                        str(triple[1]),
                        drug_c=str(triple[2]) if len(triple) > 2 else None,
                    )
                )
        return hits

    def severity_level(self, drug_a: str, drug_b: str) -> Optional[str]:
        hit = self.check_pair(drug_a, drug_b)
        if hit is None:
            return None
        return SEVERITY_TO_FINDING[hit.severity]


# --------------------------------------------------------------------------- #
# 工具函数                                                                    #
# --------------------------------------------------------------------------- #


def _to_hit(
    entry: dict[str, Any],
    drug_a: str,
    drug_b: str,
    drug_c: Optional[str] = None,
) -> InteractionHit:
    """YAML dict → InteractionHit。"""
    try:
        severity = InteractionSeverity(str(entry.get("severity", "moderate")).lower())
    except ValueError:
        severity = InteractionSeverity.MODERATE
    pair_label = f"{drug_a}+{drug_b}"
    if drug_c is not None:
        pair_label = f"{drug_a}+{drug_b}+{drug_c}"
    evidence = str(entry.get("evidence") or f"<INTERACTION_DB:{pair_label}>")
    rule_id = str(entry.get("rule_id") or f"R-INT-{pair_label}")
    note = str(entry.get("clinical_note") or "存在配伍风险，请人工复核")
    return InteractionHit(
        drug_a=drug_a,
        drug_b=drug_b,
        severity=severity,
        clinical_note=note,
        evidence=evidence,
        rule_id=rule_id,
    )


__all__ = [
    "FakeInteractionDb",
    "InteractionDbError",
    "InteractionDbProtocol",
    "InteractionHit",
    "InteractionSeverity",
    "SEVERITY_TO_FINDING",
]