"""处方解析器：HIS / 前置审方投递的 JSON 入参解析为 `Prescription` dataclass。

设计目标：
- 三种入参形态统一入口：`dict` / `str` JSON / JSON 文件路径
- 缺字段 / 非法 JSON / 缺 items[] 直接抛 `PrescriptionParseError`（不静默默认）
- `./inbox/<rx_no>.json` 文件投递模式兜底（见 `watch_inbox`）

字段顺序与校验规则对齐 `rxchk.models.prescription.from_dict`。
"""

from rxchk.parsers.prescription import (
    PrescriptionParseError,
    parse_prescription,
    watch_inbox,
)

__all__ = [
    "PrescriptionParseError",
    "parse_prescription",
    "watch_inbox",
]