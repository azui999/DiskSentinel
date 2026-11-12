"""冷备对：清单扫描与结构比对（快通道）。

设计依据 docs/冷备对功能评估.md：
- 文件夹不单独记录，目录树由清单派生；
- 清单行 = 相对路径 / 大小 / mtime / SHA-256（本轮快通道不读内容，sha256 留空）；
- 比对 = 数据库双游标按 key 排序的流式归并，内存 O(1) 附加，百万级文件不吃内存；
- 全程只读被扫描目录；结果写 SQLite（用户拍板：冷备对数据入库）。

存储：data/fileindex/index.db —— Python 标准库 sqlite3，无需安装任何东西，
数据库就是一个普通文件，拷走即备份。旧版 pairs.json 会自动导入后停用。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .paths import data_dir

# NTFS 大小写不敏感；这些名字无论大小写一律跳过
DEFAULT_EXCLUDE = {
    "$recycle.bin",
    "system volume information",
    "$windows.~ws",
    "$windows.~bt",
    "$av_",
    "config.msi",
    "msocache",
    "recovery",
}

ProgressFn = Callable[[str], None]

_COMMIT_EVERY = 50_000   # 扫描插入的分批提交粒度


class PairError(Exception):
    """冷备对相关错误。"""


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass
class Pair:
    id: int
    name: str
    path_a: str
    path_b: str
    created: str


# 差异类别
CAT_SAME = "same"
CAT_ONLY_A = "only_a"
CAT_ONLY_B = "only_b"
CAT_SIZE = "size_diff"
CAT_MTIME = "mtime_diff"

CATEGORY_TEXT = {
    CAT_ONLY_A: "只在 A",
    CAT_ONLY_B: "只在 B",
    CAT_SIZE: "大小不同",
    CAT_MTIME: "时间不同",
}


@dataclass
class DiffRow:
    category: str
    path: str
    size_a: int = -1
    size_b: int = -1
    mtime_a: int = 0
    mtime_b: int = 0


@dataclass
class CompareResult:
    counts: dict[str, int] = field(default_factory=lambda: {
        CAT_SAME: 0, CAT_ONLY_A: 0, CAT_ONLY_B: 0, CAT_SIZE: 0, CAT_MTIME: 0,
    })
    rows: list[DiffRow] = field(default_factory=list)   # 只含差异项

    @property
    def is_clean(self) -> bool:
        return self.counts[CAT_ONLY_A] == 0 and self.counts[CAT_ONLY_B] == 0 \
            and self.counts[CAT_SIZE] == 0 and self.counts[CAT_MTIME] == 0


def _fmt_ts(mtime_ns: int) -> str:
    if not mtime_ns:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime_ns / 1e9))


# --------------------------------------------------------------------------- #
# SQLite 存储
# --------------------------------------------------------------------------- #

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pairs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    path_a     TEXT NOT NULL,
    path_b     TEXT NOT NULL,
    created    TEXT NOT NULL,
    deleted_at TEXT NOT NULL DEFAULT ''   -- 非空 = 已移入回收站（软删除）
);
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id      INTEGER NOT NULL REFERENCES pairs(id),
    timestamp    TEXT NOT NULL,
    files_a      INTEGER NOT NULL DEFAULT 0,
    files_b      INTEGER NOT NULL DEFAULT 0,
    bytes_a      INTEGER NOT NULL DEFAULT 0,
    bytes_b      INTEGER NOT NULL DEFAULT 0,
    counts_json  TEXT NOT NULL DEFAULT '{}',
    scan_seconds REAL NOT NULL DEFAULT 0,
    skipped_json TEXT NOT NULL DEFAULT '{}',
    finished     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS manifests (
    run_id    INTEGER NOT NULL REFERENCES runs(id),
    side      TEXT NOT NULL,          -- 'a' / 'b'
    key       TEXT NOT NULL,          -- 小写规范路径, 比对用
    display   TEXT NOT NULL,          -- 原始大小写路径, 展示用
    size      INTEGER NOT NULL,
    mtime_ns  INTEGER NOT NULL,
    sha256    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_manifests_key ON manifests(run_id, side, key);
CREATE TABLE IF NOT EXISTS diffs (
    run_id   INTEGER NOT NULL REFERENCES runs(id),
    category TEXT NOT NULL,
    path     TEXT NOT NULL,
    size_a   INTEGER NOT NULL DEFAULT -1,
    size_b   INTEGER NOT NULL DEFAULT -1,
    mtime_a  INTEGER NOT NULL DEFAULT 0,
    mtime_b  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_diffs_run ON diffs(run_id);
"""


class PairStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else data_dir() / "fileindex"
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "index.db"
        self._init_db()
        self._import_legacy_pairs()

    # -- 连接 ------------------------------------------------------------- #
    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(_SCHEMA)
            # 老库补列：软删除标记（回收站）。表已存在时 CREATE TABLE 不会改动结构。
            self._ensure_column(conn, "pairs", "deleted_at", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "watch_targets", "deleted_at", "TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
        try:
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            return
        if columns and column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def ensure_soft_delete(self) -> None:
        """供其他模块（watch/verify）在直接用连接前确保列存在。"""
        self._init_db()

    def _import_legacy_pairs(self) -> None:
        """旧版 pairs.json（TSV 时代）一次性导入：只搬配对定义，历史 run 不搬。"""
        legacy = self.root / "pairs.json"
        if not legacy.exists():
            return
        try:
            with self.connect() as conn:
                count = conn.execute("SELECT COUNT(*) c FROM pairs").fetchone()["c"]
                if count:
                    return  # 库里已有数据，不重复导入
                data = json.loads(legacy.read_text(encoding="utf-8"))
                for item in data.get("pairs", []):
                    conn.execute(
                        "INSERT INTO pairs(name, path_a, path_b, created) VALUES(?,?,?,?)",
                        (
                            str(item.get("name") or "未命名"),
                            str(item.get("path_a") or ""),
                            str(item.get("path_b") or ""),
                            str(item.get("created") or time.strftime("%Y-%m-%d %H:%M:%S")),
                        ),
                    )
        except (OSError, ValueError, sqlite3.Error):
            return
        legacy.rename(legacy.with_suffix(".json.imported"))

    # -- 盘对 CRUD -------------------------------------------------------- #
    def pairs(self) -> list[Pair]:
        """未删除的冷备对（回收站里的不算）。"""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, name, path_a, path_b, created FROM pairs"
                " WHERE deleted_at='' ORDER BY id"
            ).fetchall()
        return [Pair(r["id"], r["name"], r["path_a"], r["path_b"], r["created"]) for r in rows]

    def deleted_pairs(self) -> list[tuple[Pair, str]]:
        """回收站里的冷备对：(配对, 删除时间)。"""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, name, path_a, path_b, created, deleted_at FROM pairs"
                " WHERE deleted_at<>'' ORDER BY deleted_at DESC, id DESC"
            ).fetchall()
        return [
            (Pair(r["id"], r["name"], r["path_a"], r["path_b"], r["created"]), r["deleted_at"])
            for r in rows
        ]

    def add(self, name: str, path_a: str, path_b: str) -> Pair:
        if not os.path.isdir(path_a):
            raise PairError(f"路径 A 不存在或不是文件夹：{path_a}")
        if not os.path.isdir(path_b):
            raise PairError(f"路径 B 不存在或不是文件夹：{path_b}")
        if os.path.abspath(path_a).lower() == os.path.abspath(path_b).lower():
            raise PairError("两个路径不能是同一个文件夹")
        created = time.strftime("%Y-%m-%d %H:%M:%S")
        with self.connect() as conn:
            existing = conn.execute("SELECT COUNT(*) c FROM pairs").fetchone()["c"]
            pair = Pair(
                id=0,
                name=name.strip() or f"冷备对 {existing + 1}",
                path_a=os.path.abspath(path_a),
                path_b=os.path.abspath(path_b),
                created=created,
            )
            cur = conn.execute(
                "INSERT INTO pairs(name, path_a, path_b, created) VALUES(?,?,?,?)",
                (pair.name, pair.path_a, pair.path_b, pair.created),
            )
            pair.id = int(cur.lastrowid or 0)
        return pair

    def remove(self, pair_id: int) -> None:
        """移入回收站（软删除）。比对历史原样保留，可从回收站恢复。"""
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE pairs SET deleted_at=? WHERE id=? AND deleted_at=''",
                (stamp, int(pair_id)),
            )
            if not cur.rowcount:
                raise PairError("没有找到这个冷备对（可能已被删除）")

    def restore(self, pair_id: int) -> None:
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE pairs SET deleted_at='' WHERE id=? AND deleted_at<>''",
                (int(pair_id),),
            )
            if not cur.rowcount:
                raise PairError("没有找到这个可恢复的冷备对")

    def purge(self, pair_id: int) -> None:
        """彻底删除配对关系及其全部历史（diffs / manifests / runs / verify 记录）。"""
        from . import verify  # 避免循环导入

        verify.ensure_schema(self)
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM diffs WHERE run_id IN (SELECT id FROM runs WHERE pair_id=?)",
                (int(pair_id),),
            )
            conn.execute(
                "DELETE FROM manifests WHERE run_id IN (SELECT id FROM runs WHERE pair_id=?)",
                (int(pair_id),),
            )
            conn.execute("DELETE FROM runs WHERE pair_id=?", (int(pair_id),))
            conn.execute(
                "DELETE FROM verify_files WHERE verify_run_id IN"
                " (SELECT id FROM verify_runs WHERE pair_id=?)", (int(pair_id),),
            )
            conn.execute("DELETE FROM verify_runs WHERE pair_id=?", (int(pair_id),))
            cur = conn.execute("DELETE FROM pairs WHERE id=?", (int(pair_id),))
            if not cur.rowcount:
                raise PairError("没有找到这个冷备对")

    def last_run(self, pair_id: int) -> dict[str, Any] | None:
        """最近一次完成的比对（runs.finished=1）。"""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE pair_id=? AND finished=1 "
                "ORDER BY id DESC LIMIT 1",
                (int(pair_id),),
            ).fetchone()
        if row is None:
            return None
        try:
            counts = json.loads(row["counts_json"])
        except ValueError:
            counts = {}
        return {
            "run_id": row["id"],
            "timestamp": row["timestamp"],
            "files_a": row["files_a"],
            "files_b": row["files_b"],
            "counts": counts,
        }

    def recent_runs(self, pair_id: int, limit: int = 2) -> list[dict[str, Any]]:
        """最近几次完成的结构比对（新→旧），用于"与上次对比"。"""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, timestamp FROM runs"
                " WHERE pair_id=? AND finished=1 ORDER BY id DESC LIMIT ?",
                (int(pair_id), int(limit)),
            ).fetchall()
        return [{"run_id": r["id"], "timestamp": r["timestamp"]} for r in rows]

    def all_runs(self, limit: int = 200) -> list[dict[str, Any]]:
        """全部结构比对记录（校验历史页用），按时间倒序。"""
        self._init_db()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT r.*, p.name AS pair_name FROM runs r"
                " JOIN pairs p ON p.id = r.pair_id"
                " WHERE r.finished=1 AND p.deleted_at=''"
                " ORDER BY r.id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        out = []
        for row in rows:
            try:
                counts = json.loads(row["counts_json"])
            except ValueError:
                counts = {}
            out.append({
                "source": "pair",
                "kind": "compare",
                "status": "finished",
                "run_id": row["id"],
                "pair_id": row["pair_id"],
                "title": row["pair_name"],
                "started": row["timestamp"],
                "finished": row["timestamp"],
                "counts": counts,
                "files": max(row["files_a"], row["files_b"]),
                "seconds": row["scan_seconds"],
            })
        return out


# --------------------------------------------------------------------------- #
# 清单扫描（直接写入 manifests 表）
# --------------------------------------------------------------------------- #

def _scan_into(
    conn: sqlite3.Connection,
    run_id: int,
    side: str,
    root: str,
    progress: ProgressFn | None,
) -> dict[str, Any]:
    """递归扫描 root，边扫边插入 manifests。返回统计（文件数/字节数/跳过项）。"""
    root_path = os.path.abspath(root)
    if not os.path.isdir(root_path):
        raise PairError(f"无法访问：{root_path}")

    started = time.perf_counter()
    files = total_bytes = 0
    skipped: list[str] = []
    buffer: list[tuple[Any, ...]] = []
    stack = [root_path]
    last_report = 0

    def flush() -> None:
        if buffer:
            conn.executemany(
                "INSERT INTO manifests(run_id, side, key, display, size, mtime_ns)"
                " VALUES(?,?,?,?,?,?)",
                buffer,
            )
            buffer.clear()

    while stack:
        folder = stack.pop()
        try:
            it = os.scandir(folder)
        except OSError as exc:
            skipped.append(f"{folder}\t{exc.strerror or exc}")
            continue
        with it:
            try:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.lower() in DEFAULT_EXCLUDE:
                                continue
                            stack.append(entry.path)
                            continue
                        stat = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        skipped.append(f"{entry.path}\t{exc.strerror or exc}")
                        continue
                    rel = os.path.relpath(entry.path, root_path).replace("\\", "/")
                    if "\t" in rel:
                        # 文件名里带制表符：罕见但会破坏比对键, 跳过并记录
                        skipped.append(f"{entry.path}\t路径含制表符, 已跳过")
                        continue
                    buffer.append((run_id, side, rel.lower(), rel, stat.st_size, stat.st_mtime_ns))
                    files += 1
                    total_bytes += stat.st_size
                    if len(buffer) >= _COMMIT_EVERY:
                        flush()
                        conn.commit()
                    if progress and files - last_report >= 20_000:
                        last_report = files
                        progress(f"扫描 {os.path.basename(root_path) or root_path}: {files:,} 个文件…")
            except OSError as exc:
                skipped.append(f"{folder}\t{exc.strerror or exc}")

    flush()
    conn.commit()
    return {
        "files": files,
        "bytes": total_bytes,
        "skipped": skipped,
        "seconds": round(time.perf_counter() - started, 1),
    }


# --------------------------------------------------------------------------- #
# 结构比对（双游标流式归并）
# --------------------------------------------------------------------------- #

def _compare_into(
    conn: sqlite3.Connection,
    run_id: int,
    progress: ProgressFn | None,
) -> CompareResult:
    tell = progress or (lambda _msg: None)
    tell("正在比对…")
    result = CompareResult()
    diff_rows: list[tuple[Any, ...]] = []

    cursor_a = conn.execute(
        "SELECT key, display, size, mtime_ns FROM manifests"
        " WHERE run_id=? AND side='a' ORDER BY key",
        (run_id,),
    )
    cursor_b = conn.execute(
        "SELECT key, display, size, mtime_ns FROM manifests"
        " WHERE run_id=? AND side='b' ORDER BY key",
        (run_id,),
    )

    fa = cursor_a.fetchone()
    fb = cursor_b.fetchone()
    while fa is not None or fb is not None:
        if fa is None:
            choice = 1
        elif fb is None:
            choice = -1
        else:
            diff = (fa["key"] > fb["key"]) - (fa["key"] < fb["key"])
            choice = diff
        if choice < 0:
            result.counts[CAT_ONLY_A] += 1
            diff_rows.append((run_id, CAT_ONLY_A, fa["display"], fa["size"], -1,
                              fa["mtime_ns"], 0))
            fa = cursor_a.fetchone()
        elif choice > 0:
            result.counts[CAT_ONLY_B] += 1
            diff_rows.append((run_id, CAT_ONLY_B, fb["display"], -1, fb["size"],
                              0, fb["mtime_ns"]))
            fb = cursor_b.fetchone()
        else:
            if fa["size"] != fb["size"]:
                result.counts[CAT_SIZE] += 1
                diff_rows.append((run_id, CAT_SIZE, fa["display"], fa["size"], fb["size"],
                                  fa["mtime_ns"], fb["mtime_ns"]))
            elif fa["mtime_ns"] != fb["mtime_ns"]:
                result.counts[CAT_MTIME] += 1
                diff_rows.append((run_id, CAT_MTIME, fa["display"], fa["size"], fb["size"],
                                  fa["mtime_ns"], fb["mtime_ns"]))
            else:
                result.counts[CAT_SAME] += 1
            fa = cursor_a.fetchone()
            fb = cursor_b.fetchone()

    if diff_rows:
        conn.executemany(
            "INSERT INTO diffs(run_id, category, path, size_a, size_b, mtime_a, mtime_b)"
            " VALUES(?,?,?,?,?,?,?)",
            diff_rows,
        )
    result.rows = [
        DiffRow(r[1], r[2], r[3], r[4], r[5], r[6]) for r in diff_rows
    ]
    return result


# --------------------------------------------------------------------------- #
# 一轮比对（UI 子线程调用）
# --------------------------------------------------------------------------- #

def run_compare(
    store: PairStore,
    pair: Pair,
    progress: ProgressFn | None = None,
) -> tuple[CompareResult, int]:
    """扫描两侧 + 比对 + 落库，一步完成。返回 (比对结果, run_id)。"""
    tell = progress or (lambda _msg: None)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with store.connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs(pair_id, timestamp) VALUES(?,?)",
            (pair.id, timestamp),
        )
        run_id = int(cur.lastrowid or 0)

        tell("正在扫描 A 侧…")
        stat_a = _scan_into(conn, run_id, "a", pair.path_a, tell)
        tell("正在扫描 B 侧…")
        stat_b = _scan_into(conn, run_id, "b", pair.path_b, tell)
        result = _compare_into(conn, run_id, tell)

        conn.execute(
            "UPDATE runs SET files_a=?, files_b=?, bytes_a=?, bytes_b=?,"
            " counts_json=?, scan_seconds=?, skipped_json=?, finished=1 WHERE id=?",
            (
                stat_a["files"], stat_b["files"],
                stat_a["bytes"], stat_b["bytes"],
                json.dumps(result.counts),
                stat_a["seconds"] + stat_b["seconds"],
                json.dumps({"a": stat_a["skipped"], "b": stat_b["skipped"]}, ensure_ascii=False),
                run_id,
            ),
        )
        conn.commit()
    return result, run_id
