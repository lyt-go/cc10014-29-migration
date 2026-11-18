"""字段类型别名注册表：旧类型简写与新类型版本的唯一权威映射。

背景：既有结构定义（schema_registry）与字段映射（column_mapping）里混用字符串简写
（STRING/INT/BIGINT/...），新结构定义版本表要求落到带版本的规范类型（VARCHAR/INTEGER/...）。
任何模块需要比较字段类型时，都必须先经过本模块归一化，禁止再直接做字符串相等比较——
否则字段映射一侧先切换、结构表仍保留简写时会持续报类型不一致。

设计要点：
- 旧写法（含大小写、空格差异）统一收敛到规范类型；
- 规范类型带「类型版本」，迁移结果记录旧别名、规范类型与类型版本；
- 兼容比较基于规范类型，旧 STRING 与新 VARCHAR 视为同一逻辑类型，不再报错；
- 未知类型不静默吞掉：标记为 UNKNOWN 并在调用处提示人工确认。
"""
from __future__ import annotations

from typing import Any

# 规范类型版本：类型体系自身的版本号，随别名表演进而递增，写入结构定义版本表。
TYPE_CATALOG_VERSION = "type-v2"

# 旧类型简写（小写去空格后匹配） -> 新规范类型
# 键全部使用小写，匹配时统一做 casefold/去空格，兼容 " string "、"String" 等存量脏写法。
LEGACY_ALIASES: dict[str, str] = {
    # 字符族：旧 STRING 简写统一迁移到新 VARCHAR 类型
    "string": "VARCHAR",
    "varchar": "VARCHAR",
    "char": "CHAR",
    "text": "VARCHAR",
    # 整数族：旧 INT 简写统一迁移到新 INTEGER 类型
    "int": "INTEGER",
    "integer": "INTEGER",
    "bigint": "BIGINT",
    "smallint": "SMALLINT",
    "tinyint": "TINYINT",
    "long": "BIGINT",
    # 浮点/精确小数族
    "decimal": "DECIMAL",
    "numeric": "DECIMAL",
    "float": "FLOAT",
    "double": "DOUBLE",
    "number": "DECIMAL",
    # 时间族
    "timestamp": "TIMESTAMP",
    "datetime": "TIMESTAMP",
    "date": "DATE",
    "time": "TIME",
    # 布尔族
    "boolean": "BOOLEAN",
    "bool": "BOOLEAN",
    # 二进制/结构化族
    "binary": "BINARY",
    "bytes": "BINARY",
    "json": "JSON",
}

# 规范类型 -> 类型版本数据结果（新类型版本）
CANONICAL_TYPE_VERSIONS: dict[str, str] = {
    canonical: TYPE_CATALOG_VERSION for canonical in set(LEGACY_ALIASES.values())
}

# 兼容别名分组：同一逻辑类型族内的规范类型互为兼容别名。
# 典型场景：同一字段存量旧 STRING 与新 VARCHAR 发生版本冲突时，按同族判定为兼容。
COMPATIBILITY_FAMILIES: list[set[str]] = [
    {"VARCHAR", "CHAR"},
    {"INTEGER", "BIGINT", "SMALLINT", "TINYINT"},
    {"DECIMAL", "FLOAT", "DOUBLE"},
    {"TIMESTAMP", "DATE", "TIME"},
]

UNKNOWN_TYPE = "UNKNOWN"


def _normalize_token(raw: Any) -> str:
    """归一化单个类型 token：去首尾空格、统一大小写用于查表。"""
    return str(raw or "").strip().casefold()


def canonical_type(raw_type: Any) -> str:
    """把任意旧/新写法映射为规范类型；无法识别时返回 UNKNOWN 交由人工确认。"""
    token = _normalize_token(raw_type)
    if not token:
        return UNKNOWN_TYPE
    # 去掉可能携带的长度/精度声明，例如 VARCHAR(255)、DECIMAL(10,2) 仍按基类型归一
    base = token.split("(", 1)[0].strip()
    return LEGACY_ALIASES.get(base, UNKNOWN_TYPE)


def type_version(raw_type: Any) -> str:
    """返回规范类型对应的新类型版本；未知类型返回空串由调用方提示。"""
    canonical = canonical_type(raw_type)
    return CANONICAL_TYPE_VERSIONS.get(canonical, "")


def is_legacy_notation(raw_type: Any) -> bool:
    """判断原始写法是否属于旧简写（用于版本表标注迁移来源）。"""
    token = _normalize_token(raw_type).split("(", 1)[0].strip()
    if not token:
        return False
    canonical = LEGACY_ALIASES.get(token, UNKNOWN_TYPE)
    # 原始写法与规范名大小写不一致即视为旧别名（如 string -> VARCHAR）
    return canonical != UNKNOWN_TYPE and token != canonical.casefold()


def split_type_list(raw_types: Any) -> list[str]:
    """结构定义里字段类型以逗号串存储，拆成逐字段 token。"""
    if raw_types is None:
        return []
    return [piece.strip() for piece in str(raw_types).split(",") if piece.strip()]


def join_canonical_types(raw_types: Any) -> str:
    """把逗号分隔的混合写法整体翻译成规范类型串。"""
    return ",".join(canonical_type(token) for token in split_type_list(raw_types))


def types_compatible(left: Any, right: Any) -> bool:
    """类型比较的唯一入口：先归一化再比较，兼容旧简写与新类型混写。

    - 两侧规范类型相同：兼容；
    - 落在同一兼容族（如 STRING→VARCHAR 与 VARCHAR）：兼容；
    - 任一侧未知：保守判定不兼容，交由人工确认，不静默放行。
    """
    left_canonical = canonical_type(left)
    right_canonical = canonical_type(right)
    if UNKNOWN_TYPE in (left_canonical, right_canonical):
        return False
    if left_canonical == right_canonical:
        return True
    for family in COMPATIBILITY_FAMILIES:
        if left_canonical in family and right_canonical in family:
            return True
    return False


def describe_type(raw_type: Any) -> dict[str, str]:
    """供结构定义版本表/接口展示的完整类型解析结果。"""
    canonical = canonical_type(raw_type)
    return {
        "raw_type": str(raw_type or ""),
        "canonical_type": canonical,
        "type_version": CANONICAL_TYPE_VERSIONS.get(canonical, ""),
        "legacy_alias": "是" if is_legacy_notation(raw_type) else "否",
        "recognized": "否" if canonical == UNKNOWN_TYPE else "是",
    }
