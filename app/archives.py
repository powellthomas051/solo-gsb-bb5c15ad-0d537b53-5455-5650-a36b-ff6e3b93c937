"""设计归档：把若干已入库设计的参数与审计结果规范化为 JSON 清单并计算
SHA-256 digest，供持久化与核验。

规范化规则（canonical JSON）：

- 清单顶层为对象，字段顺序固定：``schema`` / ``item_count`` / ``designs``；
- 请求中的设计 ID 先按升序去重处理，清单内容只取决于 ID 集合，与提交顺序、
  是否重复无关——同一批设计的 digest 必然相同，因此相同 digest 可幂等复用；
- ``designs`` 以设计 ID 字符串为键，值为该设计的入库快照（设计参数 +
  审计结果），不含 created_at 等易变元数据；
- 序列化使用 ``json.dumps(..., ensure_ascii=False, separators=(",", ":"),
  sort_keys=True)`` 并追加一个换行（与 sha256sum 类工具的习惯一致），
  digest = sha256(UTF-8 字节).hexdigest()。

核验（verify）在「当前库中重新读取设计 → 重建清单 → 重算 digest」之后与
归档时持久化的内容逐项比较，报告 digest 是否一致、哪些设计已缺失，以及
首个差异的点分路径（如 ``designs.2.audit.exceeds``）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from . import db

ARCHIVE_SCHEMA = "design-archive/v1"

# 归档快照中保留的设计参数字段（与 DesignOut 对应，去掉 id/created_at：
# id 已体现在 designs 的键上，created_at 属于易变的入库元数据）。
_DESIGN_FIELDS = (
    "name",
    "cyclic",
    "max_float",
    "shafts",
    "treadles",
    "warp_ends",
    "weft_picks",
    "threading",
    "tie_up",
    "treadling",
)
_AUDIT_FIELDS = ("exceeds", "interlacing", "violations")


def design_snapshot(design: dict) -> dict:
    """从 db.get_design 的返回值抽取归档快照（设计参数 + 审计结果）。

    只挑选固定字段、按固定顺序构造，保证快照内容只取决于入库内容本身，
    不含 id、created_at 等键（id 由清单的 designs 键承载）。
    """
    snapshot = {field: design[field] for field in _DESIGN_FIELDS}
    audit = design["audit"]
    snapshot["audit"] = {field: audit[field] for field in _AUDIT_FIELDS}
    return snapshot


def build_manifest(designs: list[dict]) -> dict:
    """按 ID 升序的设计详情列表构造规范化清单 dict。

    designs 必须已按 id 升序排列；清单的 designs 对象以 ID 字符串为键。
    """
    return {
        "schema": ARCHIVE_SCHEMA,
        "item_count": len(designs),
        "designs": {str(d["id"]): design_snapshot(d) for d in designs},
    }


def canonical_manifest_text(manifest: dict) -> str:
    """把清单序列化为规范化 JSON 文本（键排序、无多余空白、UTF-8、末尾换行）。"""
    return json.dumps(
        manifest,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"


def digest_manifest(manifest: dict) -> tuple[str, str]:
    """返回同一清单的 (canonical JSON 文本, SHA-256 hex digest)。"""
    text = canonical_manifest_text(manifest)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return text, digest


def _first_diff_path(stored, recomputed, prefix: str = "") -> str | None:
    """深度优先比较两个由 JSON 反序列化得到的值，返回首个差异的点分路径。

    - 键集合不一致时按字典序报告缺失/多出的键；
    - 两个 list 按索引比较，路径片段用 [i]；
    - 标量不等（含类型不一致）直接落在当前路径；
    - 完全一致返回 None。
    """
    if isinstance(stored, dict) and isinstance(recomputed, dict):
        for key in sorted(set(stored) | set(recomputed)):
            path = f"{prefix}.{key}" if prefix else key
            if key not in stored:
                return f"{path} (missing in stored archive)"
            if key not in recomputed:
                return f"{path} (missing in recomputed manifest)"
            sub = _first_diff_path(stored[key], recomputed[key], path)
            if sub is not None:
                return sub
        return None
    if isinstance(stored, list) and isinstance(recomputed, list):
        if len(stored) != len(recomputed):
            # 长度不一致仍继续逐元素比较，优先报告首个内容差异；内容全一致
            # 时再报告长度差异。
            for i in range(min(len(stored), len(recomputed))):
                sub = _first_diff_path(
                    stored[i], recomputed[i], f"{prefix}[{i}]"
                )
                if sub is not None:
                    return sub
            return (
                f"{prefix}: list length {len(stored)} (stored) vs "
                f"{len(recomputed)} (recomputed)"
            )
        for i, (a, b) in enumerate(zip(stored, recomputed)):
            sub = _first_diff_path(a, b, f"{prefix}[{i}]")
            if sub is not None:
                return sub
        return None
    if stored != recomputed or type(stored) is not type(recomputed):
        return prefix or "(root)"
    return None


def first_diff_path(stored_manifest: dict, recomputed_manifest: dict) -> str | None:
    """返回两份清单首个差异的点分路径；完全一致时为 None。"""
    return _first_diff_path(stored_manifest, recomputed_manifest)


def _utc_now() -> str:
    """当前 UTC 时间的 ISO 8601 秒级字符串（与 SQLite created_at 风格一致）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_archive_out(record: dict, reused: bool) -> dict:
    return {
        "id": record["id"],
        "digest": record["digest"],
        "reused": reused,
        "created_at": record["created_at"],
        "design_ids": record["design_ids"],
        "item_count": record["item_count"],
        "manifest": record["manifest"],
    }


def create_archive(design_ids: list[int]) -> tuple[dict, bool]:
    """归档一批已入库设计，返回 (归档响应 dict, 是否复用已有归档)。

    流程：ID 升序 → 全部设计必须读到（任一缺失抛 LookupError，整个请求
    失败、不产生任何归档行）→ 构造规范化清单并计算 digest → 同 digest 已
    存在则幂等复用，否则单条 INSERT 持久化。INSERT 在 digest UNIQUE 约束
    上冲突（并发下的竞争）时回退为按 digest 复用已存记录；任何失败都不会
    产生半成品归档。
    """
    ordered_ids = sorted(design_ids)
    designs = db.get_designs_by_ids(ordered_ids)
    found_ids = {d["id"] for d in designs}
    missing = [i for i in ordered_ids if i not in found_ids]
    if missing:
        raise LookupError(missing[0], missing)

    manifest = build_manifest(designs)
    manifest_text, digest = digest_manifest(manifest)

    existing = db.get_archive_by_digest(digest)
    if existing is not None:
        return _to_archive_out(existing, True), True

    try:
        record = db.insert_archive(
            digest=digest, design_ids=ordered_ids, manifest_text=manifest_text
        )
    except sqlite3.IntegrityError:
        # 并发请求抢先写入了同一 digest：事务已整体回滚，转为复用
        existing = db.get_archive_by_digest(digest)
        if existing is None:  # 理论不可达：冲突必有他者写入成功
            raise
        return _to_archive_out(existing, True), True
    return _to_archive_out(record, False), False


def verify_archive(archive_id: int) -> dict:
    """重新读取归档覆盖的设计并重建清单、重算 digest，与归档快照比对。

    归档不存在抛 db.ArchiveNotFoundError。部分设计已缺失时：
    recomputed_digest 为 null、digest_match 为 false，missing_design_ids
    列出缺失 ID，first_diff_path 指向 designs 下首个缺失项。
    """
    record = db.get_archive(archive_id)
    if record is None:
        raise db.ArchiveNotFoundError(archive_id)

    stored_manifest = record["manifest"]
    stored_ids: list[int] = list(record["design_ids"])
    designs = db.get_designs_by_ids(stored_ids)
    found_ids = {d["id"] for d in designs}
    missing_ids = [i for i in stored_ids if i not in found_ids]

    recomputed_manifest = build_manifest(designs)
    # 清单按固定键排序深度比较，首个差异天然按设计 ID 升序定位；缺失设计
    # 表现为 designs.<id> 键只存在于归档侧，由比较器标注为「重算侧缺失」。
    diff = first_diff_path(stored_manifest, recomputed_manifest)
    if missing_ids and diff is not None:
        # 把 designs.<id> 键缺失的通用标注改写为更明确的「设计已不在库中」
        for design_id in missing_ids:
            generic = f"designs.{design_id} (missing in recomputed manifest)"
            if diff == generic:
                diff = f"designs.{design_id} (design missing from store)"
                break
    # 归档行自洽性：重算持久化清单自身的 digest，与 digest 列比较，可发现
    # 「清单被改但 digest 列没改」这类存储层篡改（清单由规范化文本反序列化
    # 而来，重新规范化可还原当初计算 digest 的原始字节）。
    _, stored_self_digest = digest_manifest(stored_manifest)
    stored_self_consistent = stored_self_digest == record["digest"]
    if missing_ids:
        # 有设计缺失时无法完整重建清单：不给出重算 digest，匹配恒为 false。
        recomputed_digest = None
        digest_match = False
    else:
        _, recomputed_digest = digest_manifest(recomputed_manifest)
        digest_match = (
            recomputed_digest == record["digest"] and stored_self_consistent
        )

    return {
        "id": archive_id,
        "digest": record["digest"],
        "recomputed_digest": recomputed_digest,
        "digest_match": digest_match,
        "missing_design_ids": missing_ids,
        "first_diff_path": diff,
        "verified_at": _utc_now(),
    }
