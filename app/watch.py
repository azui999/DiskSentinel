"""单盘自检（P3）：一块盘（或某个目录）与它自己的历史基线比对。

为什么需要它（docs/冷备对功能评估.md 的"第三票"）：
盘对校验要求 A、B 两块盘同时在线，而且 A、B 内容不同时无法判断坏的是哪一块。
单盘自检解决这两个问题——
- 只带一块盘回家插上就能复检，不需要另一块盘；
- 跟自己的过去比，能确定"是这块盘在退化"。

核心判定：**内容损坏（corrupt）**
文件大小和修改时间都没变，但 SHA-256 变了。文件系统完全不知道这件事发生过，
只有逐字节读出来重算才能发现——这就是你说的"照片下半部分变成纯色"。
这是本工具存在的理由，所以它单独成一类，用最高等级告警。

基线为什么不滚动更新：
每次复检的结果如果自动成为下次的基线，坏文件就只会被报一次，
从第二次起基线里已经存着坏哈希，"问题"被静默吞掉。
所以基线一旦建立就固定（watch_targets.baseline_run_id），
只有用户显式点【重建基线】才更新。

存储：与冷备对共用 data/fileindex/index.db（同一个文件，备份一次就够）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as futures_wait
from dataclasses import dataclass, field
from typing import Any, Callable

from .pairing import PairError, PairStore
from .verify import Control, Paused, _hash_file, _walk

ProgressFn = Callable[[dict[str, Any]], None]

# 复检结果类别
CAT_SAME = "same"
CAT_NEW = "new"
CAT_MISSING = "missing"
CAT_MODIFIED = "modified"
CAT_CORRUPT = "corrupt"
CAT_UNREADABLE = "unreadable"

CATEGORY_TEXT = {
    CAT_NEW: "新增",
    CAT_MISSING: "丢失",
    CAT_MODIFIED: "已修改",
    CAT_CORRUPT: "内容损坏",
    CAT_UNREADABLE: "无法读取",
}

_HASH_WORKERS = 6
_QUEUE_DEPTH = 24


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass
class WatchTarget:
    id: int
    name: str
    path: str
    created: str
    baseline_run_id: int | None = None


@dataclass
class WatchResult:
    counts: dict[str, int] = field(default_factory=lambda: {
        CAT_SAME: 0, CAT_NEW: 0, CAT_MISSING: 0, CAT_MODIFIED: 0,
        CAT_CORRUPT: 0, CAT_UNREADABLE: 0,
    })
    rows: list[dict[str, Any]] = field(default_factory=list)   # 只含差异项
    baseline_created: bool = False   # 本次运行是否同时建立了基线
    files: int = 0
    bytes: int = 0
    hashed_files: int = 0
    reused_files: int = 0
    read_bytes: int = 0
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def is_clean(self) -> bool:
        """没有丢失、没有内容损坏、没有读不出来的文件。新增/已修改属于正常使用痕迹。"""
        return (
            self.counts[CAT_MISSING] == 0
            and self.counts[CAT_CORRUPT] == 0
            and self.counts[CAT_UNREADABLE] == 0
        )

    @property
    def has_corruption(self) -> bool:
        return self.counts[CAT_CORRUPT] > 0


# --------------------------------------------------------------------------- #
# 表结构
# --------------------------------------------------------------------------- #

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watch_targets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    path            TEXT NOT NULL UNIQUE,
    created         TEXT NOT NULL,
    baseline_run_id INTEGER,
    deleted_at      TEXT NOT NULL DEFAULT ''   -- 非空 = 已移入回收站（软删除）
);
CREATE TABLE IF NOT EXISTS watch_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id        INTEGER NOT NULL REFERENCES watch_targets(id),
    kind             TEXT NOT NULL DEFAULT 'check',   -- baseline / check
    status           TEXT NOT NULL DEFAULT 'running', -- running / paused / finished
    started          TEXT NOT NULL,
    finished         TEXT NOT NULL DEFAULT '',
    baseline_run_id  INTEGER NOT NULL DEFAULT 0,
    files            INTEGER NOT NULL DEFAULT 0,
    bytes            INTEGER NOT NULL DEFAULT 0,
    counts_json      TEXT NOT NULL DEFAULT '{}',
    reused_files     INTEGER NOT NULL DEFAULT 0,
    hashed_files     INTEGER NOT NULL DEFAULT 0,
    read_bytes       INTEGER NOT NULL DEFAULT 0,
    seconds          REAL NOT NULL DEFAULT 0,
    errors_json      TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_watch_runs_target ON watch_runs(target_id, id);
CREATE TABLE IF NOT EXISTS watch_files (
    run_id   INTEGER NOT NULL REFERENCES watch_runs(id),
    key      TEXT NOT NULL,
    display  TEXT NOT NULL,
    size     INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    sha256   TEXT NOT NULL DEFAULT '',
    error    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, key)
) WITHOUT ROWID;
"""


def ensure_schema(store: PairStore) -> None:
    with store.connect() as conn:
        conn.executescript(_SCHEMA)
    # 老库/新库统一补上软删除列（回收站功能依赖）
    store.ensure_soft_delete()


# --------------------------------------------------------------------------- #
# 目标（一块盘 / 一个目录）管理
# --------------------------------------------------------------------------- #

class WatchStore:
    """单盘自检目标的增删查。复用 PairStore 的连接与数据库文件。"""

    def __init__(self, store: PairStore) -> None:
        self.store = store
        ensure_schema(store)

    def connect(self) -> sqlite3.Connection:
        return self.store.connect()

    def targets(self) -> list[WatchTarget]:
        """未删除的自检目标（回收站里的不算）。"""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, name, path, created, baseline_run_id"
                " FROM watch_targets WHERE deleted_at='' ORDER BY id"
            ).fetchall()
        return [
            WatchTarget(r["id"], r["name"], r["path"], r["created"], r["baseline_run_id"])
            for r in rows
        ]

    def deleted_targets(self) -> list[tuple[WatchTarget, str]]:
        """回收站里的自检目标：(目标, 删除时间)。"""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, name, path, created, baseline_run_id, deleted_at"
                " FROM watch_targets WHERE deleted_at<>''"
                " ORDER BY deleted_at DESC, id DESC"
            ).fetchall()
        return [
            (
                WatchTarget(r["id"], r["name"], r["path"], r["created"], r["baseline_run_id"]),
                r["deleted_at"],
            )
            for r in rows
        ]

    def add(self, name: str, path: str) -> WatchTarget:
        if not os.path.isdir(path):
            raise PairError(f"路径不存在或不是文件夹：{path}")
        absolute = os.path.abspath(path)
        with self.connect() as conn:
            exists = conn.execute(
                "SELECT id FROM watch_targets WHERE path=? COLLATE NOCASE AND deleted_at=''",
                (absolute,),
            ).fetchone()
            if exists:
                raise PairError(f"这个路径已经添加过了：{absolute}")
            # path 有 UNIQUE 约束：回收站里可能有同路径的软删除行，
            # 直接复用那一行（历史记录也一起回来），避免撞唯一索引
            recycled = conn.execute(
                "SELECT id FROM watch_targets WHERE path=? COLLATE NOCASE AND deleted_at<>''",
                (absolute,),
            ).fetchone()
            count = conn.execute(
                "SELECT COUNT(*) c FROM watch_targets WHERE deleted_at=''"
            ).fetchone()["c"]
            display_name = name.strip() or f"自检目标 {count + 1}"
            if recycled:
                conn.execute(
                    "UPDATE watch_targets SET deleted_at='', name=? WHERE id=?",
                    (display_name, int(recycled["id"])),
                )
                row = conn.execute(
                    "SELECT id, name, path, created, baseline_run_id"
                    " FROM watch_targets WHERE id=?",
                    (int(recycled["id"]),),
                ).fetchone()
                return WatchTarget(row["id"], row["name"], row["path"],
                                   row["created"], row["baseline_run_id"])
            cur = conn.execute(
                "INSERT INTO watch_targets(name, path, created) VALUES(?,?,?)",
                (display_name, absolute, time.strftime("%Y-%m-%d %H:%M:%S")),
            )
            target_id = int(cur.lastrowid or 0)
        return WatchTarget(target_id, display_name,
                           absolute, time.strftime("%Y-%m-%d %H:%M:%S"), None)

    def remove(self, target_id: int) -> None:
        """移入回收站（软删除）。基线与历次复检结果原样保留，可恢复。"""
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE watch_targets SET deleted_at=? WHERE id=? AND deleted_at=''",
                (stamp, int(target_id)),
            )
            if not cur.rowcount:
                raise PairError("没有找到这个自检目标（可能已被删除）")

    def restore(self, target_id: int) -> None:
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE watch_targets SET deleted_at='' WHERE id=? AND deleted_at<>''",
                (int(target_id),),
            )
            if not cur.rowcount:
                raise PairError("没有找到这个可恢复的自检目标")

    def purge(self, target_id: int) -> None:
        """彻底删除目标及其全部自检记录（基线 + 历次复检）。"""
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM watch_files WHERE run_id IN"
                " (SELECT id FROM watch_runs WHERE target_id=?)", (int(target_id),)
            )
            conn.execute("DELETE FROM watch_runs WHERE target_id=?", (int(target_id),))
            cur = conn.execute("DELETE FROM watch_targets WHERE id=?", (int(target_id),))
            if not cur.rowcount:
                raise PairError("没有找到这个自检目标")

    def reset_baseline(self, target_id: int) -> None:
        """清掉基线，下次复检重新建立（用于"文件都换过一批"的场合）。"""
        with self.connect() as conn:
            conn.execute(
                "UPDATE watch_targets SET baseline_run_id=NULL WHERE id=?", (int(target_id),)
            )

    def last_run(self, target_id: int) -> dict[str, Any] | None:
        """最近一次完成的复检（含摘要），没有基线时返回 None。"""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM watch_runs WHERE target_id=? AND status='finished'"
                " ORDER BY id DESC LIMIT 1",
                (int(target_id),),
            ).fetchone()
        if row is None:
            return None
        try:
            counts = json.loads(row["counts_json"])
        except ValueError:
            counts = {}
        return {
            "run_id": row["id"],
            "kind": row["kind"],
            "timestamp": row["finished"],
            "counts": counts,
            "files": row["files"],
            "bytes": row["bytes"],
            "hashed_files": row["hashed_files"],
            "reused_files": row["reused_files"],
            "seconds": row["seconds"],
        }

    def recent_checks(self, target_id: int, limit: int = 2) -> list[dict[str, Any]]:
        """最近几次完成的复检（不含建立基线那次），用于"与上次对比"。"""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, finished, started FROM watch_runs"
                " WHERE target_id=? AND status='finished' AND kind='check'"
                " ORDER BY id DESC LIMIT ?",
                (int(target_id), int(limit)),
            ).fetchall()
        return [
            {"run_id": r["id"], "timestamp": r["finished"] or r["started"]} for r in rows
        ]

    def history(self, target_id: int, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM watch_runs WHERE target_id=? ORDER BY id DESC LIMIT ?",
                (int(target_id), int(limit)),
            ).fetchall()
        out = []
        for row in rows:
            try:
                counts = json.loads(row["counts_json"])
            except ValueError:
                counts = {}
            out.append({
                "run_id": row["id"],
                "kind": row["kind"],
                "status": row["status"],
                "started": row["started"],
                "finished": row["finished"],
                "counts": counts,
                "files": row["files"],
                "bytes": row["bytes"],
                "hashed_files": row["hashed_files"],
                "reused_files": row["reused_files"],
                "seconds": row["seconds"],
            })
        return out


# --------------------------------------------------------------------------- #
# 复检主流程（UI 子线程调用）
# --------------------------------------------------------------------------- #

def verify_target(
    watch: WatchStore,
    target: WatchTarget,
    control: Control,
    progress: ProgressFn | None = None,
) -> WatchResult:
    """扫描 + 全量哈希 + 与固定基线比对。

    首次运行只建立基线（counts 全为 0，baseline_created=True）；
    之后每次都与基线比，报出 新增 / 丢失 / 已修改 / 内容损坏。
    """
    tell = progress or (lambda _info: None)
    started = time.perf_counter()
    result = WatchResult()

    conn = watch.connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        baseline = _baseline_run_id(conn, target.id)
        resume = _load_resume_hashes(conn, target.id)

        cur = conn.execute(
            "INSERT INTO watch_runs(target_id, kind, started, baseline_run_id)"
            " VALUES(?,?,?,?)",
            (target.id, "check" if baseline else "baseline",
             time.strftime("%Y-%m-%d %H:%M:%S"), baseline or 0),
        )
        run_id = int(cur.lastrowid or 0)
        conn.commit()

        try:
            _hash_into(conn, run_id, target.path, resume, result, control, tell)
        except Paused:
            result.seconds = time.perf_counter() - started
            _finish_run(conn, run_id, result, status="paused")
            raise

        if baseline:
            _compare_with_baseline(conn, run_id, baseline, result, tell)
        else:
            result.baseline_created = True
            result.counts[CAT_SAME] = result.files

        result.seconds = time.perf_counter() - started
        _finish_run(conn, run_id, result, status="finished")
        if not baseline:
            conn.execute(
                "UPDATE watch_targets SET baseline_run_id=? WHERE id=?",
                (run_id, target.id),
            )
            conn.commit()
    finally:
        conn.close()
    return result


def _baseline_run_id(conn: sqlite3.Connection, target_id: int) -> int:
    row = conn.execute(
        "SELECT baseline_run_id FROM watch_targets WHERE id=?", (int(target_id),)
    ).fetchone()
    if row is None or not row["baseline_run_id"]:
        return 0
    # 基线 run 必须真的存在且完整，否则退回"无基线"
    ok = conn.execute(
        "SELECT 1 FROM watch_runs WHERE id=? AND status='finished'",
        (int(row["baseline_run_id"]),),
    ).fetchone()
    return int(row["baseline_run_id"]) if ok else 0


def _load_resume_hashes(
    conn: sqlite3.Connection, target_id: int
) -> dict[str, tuple[str, int, int]]:
    """只能沿用**没跑完**那次复检的哈希（断点续跑）。

    绝不能沿用已完成的复检：静默损坏不改大小和时间，
    从旧结果借用哈希 = 这个文件根本没被检查过，却显示"一致"。
    """
    reuse: dict[str, tuple[str, int, int]] = {}
    rows = conn.execute(
        "SELECT wf.key, wf.sha256, wf.size, wf.mtime_ns"
        " FROM watch_files wf JOIN watch_runs wr ON wr.id = wf.run_id"
        " WHERE wr.target_id=? AND wr.status IN ('running','paused')"
        " AND wf.sha256 != '' AND wf.error = ''",
        (int(target_id),),
    )
    for r in rows:
        reuse[r["key"]] = (r["sha256"], r["size"], r["mtime_ns"])
    return reuse


def _hash_into(
    conn: sqlite3.Connection,
    run_id: int,
    root: str,
    resume: dict[str, tuple[str, int, int]],
    result: WatchResult,
    control: Control,
    tell: ProgressFn,
) -> None:
    entries, skipped = _walk(root)
    result.files = len(entries)
    result.bytes = sum(e[2] for e in entries)
    if skipped:
        result.errors.extend(skipped[:200])

    conn.executemany(
        "INSERT OR REPLACE INTO watch_files(run_id, key, display, size, mtime_ns)"
        " VALUES(?,?,?,?,?)",
        [(run_id, k, d, s, m) for k, d, s, m in entries],
    )
    conn.commit()

    done_bytes = 0
    started = time.perf_counter()
    last_report = started

    with ThreadPoolExecutor(max_workers=_HASH_WORKERS) as pool:
        pending: dict[Any, tuple[str, str, int, int]] = {}
        iterator = iter(entries)

        def submit_more() -> bool:
            nonlocal done_bytes
            while len(pending) < _QUEUE_DEPTH:
                try:
                    entry = next(iterator)
                except StopIteration:
                    return bool(pending)
                key, display, size, mtime = entry
                cached = resume.get(key)
                if cached and cached[1] == size and cached[2] == mtime:
                    conn.execute(
                        "UPDATE watch_files SET sha256=?"
                        " WHERE run_id=? AND key=?", (cached[0], run_id, key)
                    )
                    result.reused_files += 1
                    done_bytes += size
                    continue
                control.check()
                pending[pool.submit(_hash_file, os.path.join(root, display))] = entry
            return True

        keep = submit_more()
        while pending:
            control.check()
            done, _ = futures_wait(pending, return_when="FIRST_COMPLETED")
            for future in done:
                key, display, size, mtime = pending.pop(future)
                sha256, error = future.result()
                conn.execute(
                    "UPDATE watch_files SET sha256=?, error=? WHERE run_id=? AND key=?",
                    (sha256, error, run_id, key),
                )
                result.hashed_files += 1
                result.read_bytes += size
                done_bytes += size
                if error:
                    result.errors.append(f"{display}\t{error}")
            conn.commit()
            keep = submit_more()

            now = time.perf_counter()
            if now - last_report >= 0.5:
                last_report = now
                elapsed = max(now - started, 1e-6)
                rate = done_bytes / elapsed
                tell({
                    "phase": "hash", "side": "", "files": result.hashed_files,
                    "reused": result.reused_files, "bytes": done_bytes,
                    "total_bytes": result.bytes, "rate": rate,
                    "eta": (result.bytes - done_bytes) / max(rate, 1.0),
                })
            if not keep and not pending:
                break
    conn.commit()


def _compare_with_baseline(
    conn: sqlite3.Connection,
    run_id: int,
    baseline: int,
    result: WatchResult,
    tell: ProgressFn,
) -> None:
    """当前 run 与基线比对。两次 SQL：主比对（含新增/修改/损坏）+ 丢失。"""
    tell({"phase": "compare", "side": "", "files": result.hashed_files,
          "reused": result.reused_files, "bytes": 0, "total_bytes": 0,
          "rate": 0.0, "eta": 0.0})

    rows = conn.execute(
        "SELECT cur.key AS key, cur.display AS display, cur.size AS size,"
        " cur.mtime_ns AS mtime, cur.sha256 AS sha, cur.error AS err,"
        " b.sha256 AS base_sha, b.size AS base_size, b.mtime_ns AS base_mtime"
        " FROM watch_files cur"
        " LEFT JOIN watch_files b ON b.run_id=? AND b.key=cur.key"
        " WHERE cur.run_id=?",
        (baseline, run_id),
    )

    for r in rows:
        if r["err"]:
            # 连读都读不出来：可能是坏道，也可能文件被其他程序独占。
            # 单独一类，不能和"内容变了"混在一起——处理方式完全不同。
            result.counts[CAT_UNREADABLE] += 1
            result.rows.append({
                "category": CAT_UNREADABLE, "path": r["display"], "size": r["size"],
                "mtime": r["mtime"], "sha": "", "base_sha": "",
                "note": f"读取失败：{r['err']}",
            })
            continue

        if r["base_size"] is None:
            result.counts[CAT_NEW] += 1
            result.rows.append({
                "category": CAT_NEW, "path": r["display"], "size": r["size"],
                "mtime": r["mtime"], "sha": r["sha"], "base_sha": "",
                "note": "基线里没有这个文件",
            })
            continue

        changed_meta = r["size"] != r["base_size"] or r["mtime"] != r["base_mtime"]
        if changed_meta:
            result.counts[CAT_MODIFIED] += 1
            result.rows.append({
                "category": CAT_MODIFIED, "path": r["display"], "size": r["size"],
                "mtime": r["mtime"], "sha": r["sha"], "base_sha": r["base_sha"],
                "note": "大小或修改时间变了",
            })
        elif r["sha"] and r["base_sha"] and r["sha"] != r["base_sha"]:
            result.counts[CAT_CORRUPT] += 1
            result.rows.append({
                "category": CAT_CORRUPT, "path": r["display"], "size": r["size"],
                "mtime": r["mtime"], "sha": r["sha"], "base_sha": r["base_sha"],
                "note": "大小和时间都没变，但内容变了",
            })
        else:
            result.counts[CAT_SAME] += 1

    missing = conn.execute(
        "SELECT b.display AS display, b.size AS size, b.mtime_ns AS mtime,"
        " b.sha256 AS base_sha FROM watch_files b"
        " WHERE b.run_id=? AND NOT EXISTS"
        " (SELECT 1 FROM watch_files c WHERE c.run_id=? AND c.key=b.key)",
        (baseline, run_id),
    )
    for r in missing:
        result.counts[CAT_MISSING] += 1
        result.rows.append({
            "category": CAT_MISSING, "path": r["display"], "size": r["size"],
            "mtime": r["mtime"], "sha": "", "base_sha": r["base_sha"],
            "note": "基线里有，现在找不到了",
        })


def _finish_run(
    conn: sqlite3.Connection, run_id: int, result: WatchResult, status: str
) -> None:
    conn.execute(
        "UPDATE watch_runs SET status=?, finished=?, files=?, bytes=?, counts_json=?,"
        " reused_files=?, hashed_files=?, read_bytes=?, seconds=?, errors_json=?"
        " WHERE id=?",
        (
            status, time.strftime("%Y-%m-%d %H:%M:%S"), result.files, result.bytes,
            json.dumps(result.counts), result.reused_files, result.hashed_files,
            result.read_bytes, result.seconds,
            json.dumps(result.errors[:200], ensure_ascii=False)[:8000], run_id,
        ),
    )
    conn.commit()


def all_runs(store: PairStore, limit: int = 200) -> list[dict[str, Any]]:
    """全部单盘自检记录，按时间倒序（含各次统计）。"""
    ensure_schema(store)
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT wr.*, wt.name AS target_name FROM watch_runs wr"
            " JOIN watch_targets wt ON wt.id = wr.target_id"
            " WHERE wt.deleted_at='' ORDER BY wr.id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    out = []
    for row in rows:
        try:
            counts = json.loads(row["counts_json"])
        except ValueError:
            counts = {}
        out.append({
            "source": "watch",
            "run_id": row["id"],
            "title": row["target_name"],
            "status": row["status"],
            "kind": row["kind"],
            "started": row["started"],
            "finished": row["finished"],
            "counts": counts,
            "files": row["files"],
            "seconds": row["seconds"],
        })
    return out


def result_rows(watch: WatchStore, run_id: int) -> list[dict[str, Any]]:
    """事后重算某次复检的差异明细（历史报告用）。

    明细不另存一份：run 与它的基线都在 watch_files 里，随时可重放。
    只读一次库，不碰磁盘。
    """
    conn = watch.connect()
    try:
        row = conn.execute(
            "SELECT baseline_run_id FROM watch_runs WHERE id=?", (int(run_id),)
        ).fetchone()
        if row is None or not row["baseline_run_id"]:
            return []
        temp = WatchResult()
        _compare_with_baseline(conn, int(run_id), int(row["baseline_run_id"]),
                               temp, lambda _i: None)
        return temp.rows
    finally:
        conn.close()
