"""盘对差异的树形数据：把两侧清单合并成一棵"两边行数完全一致"的目录树。

为什么合并成一棵树：
  左侧树和右侧树必须是同一套行结构，同步滚动才有意义——滚动到第 N 行，
  两边指的还是同一个文件夹里的同一个文件。所以这里先按"两侧路径的并集"
  建一次树，再给每个节点挂上 A/B 两侧各自的大小、时间、指纹。

数据只从 index.db 读，不碰硬盘。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Iterator

MISSING = -1

# 分类 -> 通用文案（两侧共用）
CAT_TEXT = {
    "only_a": "只在 A 侧",
    "only_b": "只在 B 侧",
    "size_diff": "大小不同",
    "mtime_diff": "时间不同",
    "content_diff": "内容不同",
    # 单盘自检（当前 vs 基线）
    "new": "新增",
    "missing": "丢失",
    "modified": "已修改",
    "corrupt": "内容损坏",
    "unreadable": "无法读取",
}

# 严重程度：2=数据可能已经坏了（红），1=两侧/现状对不上（橙），0=正常变动（灰蓝）
SEVERITY = {
    "content_diff": 2,
    "size_diff": 2,
    "corrupt": 2,
    "unreadable": 2,
    "missing": 1,
    "only_a": 1,
    "only_b": 1,
    "new": 0,
    "modified": 0,
    "mtime_diff": 0,
}


@dataclass
class FileNode:
    """一个文件的双方视角。size_a/b 为 -1 表示该侧没有这个文件。"""

    name: str
    path: str
    size_a: int = MISSING
    size_b: int = MISSING
    mtime_a: int = 0
    mtime_b: int = 0
    sha_a: str = ""
    sha_b: str = ""
    issue: str = ""  # "" 表示两侧一致

    @property
    def has_problem(self) -> bool:
        return bool(self.issue)

    @property
    def sev(self) -> int:
        """-1 = 没问题；0/1/2 见 SEVERITY。"""
        if not self.issue:
            return -1
        return SEVERITY.get(self.issue, 1)

    def side_size(self, side: str) -> int:
        return self.size_a if side == "a" else self.size_b

    def side_mtime(self, side: str) -> int:
        return self.mtime_a if side == "a" else self.mtime_b

    def side_sha(self, side: str) -> str:
        return self.sha_a if side == "a" else self.sha_b

    def exists(self, side: str) -> bool:
        return self.side_size(side) != MISSING

    def issue_text(self, side: str) -> str:
        """这一侧要显示的"关键问题"，其余信息留给 tooltip / 详情面板。"""
        if not self.issue:
            return ""
        if self.issue == "only_a":
            return "只在 A 侧" if side == "a" else "本侧缺失"
        if self.issue == "only_b":
            return "只在 B 侧" if side == "b" else "本侧缺失"
        if self.issue == "size_diff":
            return "大小不同"
        if self.issue == "mtime_diff":
            return "时间不同"
        if self.issue == "content_diff":
            return "内容不同"
        return CAT_TEXT.get(self.issue, self.issue)


@dataclass
class DirNode:
    name: str
    path: str
    dirs: dict[str, "DirNode"] = field(default_factory=dict)
    files: dict[str, FileNode] = field(default_factory=dict)
    direct_problems: int = 0   # 本级直接的问题文件数
    total_problems: int = 0    # 含所有子目录的问题文件数
    worst_sev: int = -1        # 子树中最严重的问题等级（-1 = 无问题）

    @property
    def has_problem(self) -> bool:
        return self.total_problems > 0

    @property
    def total_files(self) -> int:
        return len(self.files) + sum(child.total_files for child in self.dirs.values())

    def sorted_dirs(self) -> list["DirNode"]:
        # 有问题的目录排前面，其余按名字排——用户先看到该处理的东西
        return sorted(self.dirs.values(), key=lambda d: (not d.has_problem, d.name.lower()))

    def sorted_files(self) -> list[FileNode]:
        return sorted(self.files.values(), key=lambda f: (not f.has_problem, f.name.lower()))


def build(files: Iterable[FileNode]) -> DirNode:
    """按路径并集建树。"""
    root = DirNode("", "")
    for item in files:
        node = root
        parts = [p for p in item.path.split("/") if p]
        for name in parts[:-1]:
            child = node.dirs.get(name.lower())
            if child is None:
                child = DirNode(name, f"{node.path}/{name}" if node.path else name)
                node.dirs[name.lower()] = child
            node = child
        node.files[item.name.lower()] = item
    _count_problems(root)
    return root


def _count_problems(node: DirNode) -> int:
    node.direct_problems = sum(1 for f in node.files.values() if f.has_problem)
    total = node.direct_problems
    worst = max((f.sev for f in node.files.values() if f.has_problem), default=-1)
    for child in node.dirs.values():
        total += _count_problems(child)
        worst = max(worst, child.worst_sev)
    node.total_problems = total
    node.worst_sev = worst
    return total


# ---------------------------------------------------------------------- #
# 从数据库装载
# ---------------------------------------------------------------------- #
def _row(r: sqlite3.Row | None) -> tuple[int, int, str]:
    if r is None:
        return MISSING, 0, ""
    return int(r["size"]), int(r["mtime_ns"]), str(r["sha256"] or "")


def _merge(
    cur_a: sqlite3.Iterator | sqlite3.Cursor,
    cur_b: sqlite3.Iterator | sqlite3.Cursor,
    issues: dict[str, str],
    with_mtime: bool,
) -> Iterator[FileNode]:
    """两路按 key 升序的游标流式归并成 FileNode（内存 O(1)）。"""
    fa = cur_a.fetchone()
    fb = cur_b.fetchone()
    while fa is not None or fb is not None:
        if fa is None:
            choice = 1
        elif fb is None:
            choice = -1
        else:
            choice = (fa["key"] > fb["key"]) - (fa["key"] < fb["key"])

        if choice < 0:
            display = str(fa["display"])
            size_a, mtime_a, sha_a = _row(fa)
            size_b, mtime_b, sha_b = MISSING, 0, ""
            fa = cur_a.fetchone()
        elif choice > 0:
            display = str(fb["display"])
            size_a, mtime_a, sha_a = MISSING, 0, ""
            size_b, mtime_b, sha_b = _row(fb)
            fb = cur_b.fetchone()
        else:
            display = str(fa["display"])
            size_a, mtime_a, sha_a = _row(fa)
            size_b, mtime_b, sha_b = _row(fb)
            fa = cur_a.fetchone()
            fb = cur_b.fetchone()

        if not with_mtime:
            mtime_a = mtime_b = 0
        name = display.rsplit("/", 1)[-1]
        yield FileNode(
            name=name,
            path=display,
            size_a=size_a,
            size_b=size_b,
            mtime_a=mtime_a,
            mtime_b=mtime_b,
            sha_a=sha_a,
            sha_b=sha_b,
            issue=issues.get(display.lower(), ""),
        )


def load_compare(store, run_id: int) -> DirNode:
    """结构比对的 run：manifest（全量清单）+ diffs（问题分类）。"""
    issues: dict[str, str] = {}
    with store.connect() as conn:
        for row in conn.execute("SELECT path, category FROM diffs WHERE run_id=?", (int(run_id),)):
            if str(row["category"]) == "mtime_diff":
                continue  # 旧版把"修改时间不同"也当问题，现在不算
            issues[str(row["path"]).lower()] = str(row["category"])
        cur_a = conn.execute(
            "SELECT key, display, size, mtime_ns, '' AS sha256 FROM manifests"
            " WHERE run_id=? AND side='a' ORDER BY key",
            (int(run_id),),
        )
        cur_b = conn.execute(
            "SELECT key, display, size, mtime_ns, '' AS sha256 FROM manifests"
            " WHERE run_id=? AND side='b' ORDER BY key",
            (int(run_id),),
        )
        return build(list(_merge(cur_a, cur_b, issues, with_mtime=True)))


def load_verify(store, run_id: int) -> DirNode:
    """内容校验的 run：verify_files（全量清单 + 指纹）+ 现算的问题分类。"""
    from . import verify  # 延迟导入，避免循环

    issues: dict[str, str] = {}
    for row in verify.run_rows(store, int(run_id)):
        if str(row["category"]) == "mtime_diff":
            continue  # 旧版把"修改时间不同"也当问题，现在不算
        issues[str(row["path"]).lower()] = str(row["category"])

    with store.connect() as conn:
        cur_a = conn.execute(
            "SELECT key, display, size, mtime_ns, sha256 FROM verify_files"
            " WHERE verify_run_id=? AND side='a' ORDER BY key",
            (int(run_id),),
        )
        cur_b = conn.execute(
            "SELECT key, display, size, mtime_ns, sha256 FROM verify_files"
            " WHERE verify_run_id=? AND side='b' ORDER BY key",
            (int(run_id),),
        )
        return build(list(_merge(cur_a, cur_b, issues, with_mtime=True)))


def load_latest(store, pair_id: int) -> tuple[DirNode | None, str]:
    """优先用最近一次内容校验（信息最全），否则退回结构比对。

    返回 (树, 来源说明)。
    """
    from . import verify

    last = verify.last_verify(store, pair_id)
    if last:
        return load_verify(store, last["verify_run_id"]), "内容校验"
    run = store.last_run(pair_id)
    if run:
        return load_compare(store, run["run_id"]), "结构比对"
    return None, ""


def load_watch(store, run_id: int) -> DirNode | None:
    """单盘自检的 run：当前清单（watch_files 当前 run）vs 基线清单。

    基线 run 还没建立时（kind=baseline），树里只有当前清单、没有任何问题。
    """
    from . import watch  # 延迟导入，避免循环

    issues: dict[str, str] = {}
    try:
        for row in watch.result_rows(store, int(run_id)):
            issues[str(row["path"]).lower()] = str(row["category"])
    except Exception:  # noqa: BLE001 - 重放失败时退化为"只显示清单不显示问题"
        issues = {}

    with store.connect() as conn:
        run_row = conn.execute(
            "SELECT baseline_run_id FROM watch_runs WHERE id=?", (int(run_id),)
        ).fetchone()
        baseline = int(run_row["baseline_run_id"]) if run_row else 0

        cur_a = conn.execute(
            "SELECT key, display, size, mtime_ns, sha256 FROM watch_files"
            " WHERE run_id=? ORDER BY key",
            (int(run_id),),
        )
        if baseline:
            cur_b = conn.execute(
                "SELECT key, display, size, mtime_ns, sha256 FROM watch_files"
                " WHERE run_id=? ORDER BY key",
                (baseline,),
            )
            return build(list(_merge(cur_a, cur_b, issues, with_mtime=True)))

        # 还没有基线：只有当前清单，全部视为正常
        nodes = []
        for r in cur_a:
            display = str(r["display"])
            nodes.append(
                FileNode(
                    name=display.rsplit("/", 1)[-1],
                    path=display,
                    size_a=int(r["size"]),
                    mtime_a=int(r["mtime_ns"]),
                    sha_a=str(r["sha256"] or ""),
                )
            )
        return build(nodes)


def advice(node: FileNode) -> str:
    """一句"该怎么办"，只给具体动作。"""
    if node.issue == "only_a":
        return "A 侧有这个文件、B 侧没有：检查是否漏拷，需要的话从 A 复制到 B。"
    if node.issue == "only_b":
        return "B 侧有这个文件、A 侧没有：确认它是新增的、还是 A 侧被误删。"
    if node.issue == "size_diff":
        return "同名文件两侧大小不同，通常是一次拷贝没完成或文件被截断；以较大且能正常打开的一侧为准。"
    if node.issue == "content_diff":
        return (
            "大小相同，但 SHA-256 指纹不同——内容确实不一样。可能是静默损坏"
            "（如照片下半部分变纯色），也可能是文件被改过。"
            "先打开确认哪一侧是想要的，再用完好的一侧覆盖另一侧。"
        )
    if node.issue == "mtime_diff":
        return "大小相同、修改时间不同，通常是重新保存或复制方式不同导致，内容很可能一致；介意的话跑一次内容校验确认。"
    # 单盘自检（当前 vs 基线）
    if node.issue == "corrupt":
        return (
            "大小和修改时间都没变，但内容与基线不一致——这是磁盘位翻转的典型特征。"
            "用备份覆盖这个文件，覆盖后再复检一次确认恢复。"
        )
    if node.issue == "unreadable":
        return "读取失败：可能是坏道、文件被其他程序占用或权限问题。先重试一次复检，仍失败就要考虑从备份恢复。"
    if node.issue == "missing":
        return "基线里有这个文件、现在找不到：确认是自己删除/移动的；不是的话要警惕盘有问题，尽快从备份恢复。"
    if node.issue == "modified":
        return "大小或修改时间变了，通常是正常编辑或重新保存；内容是否完好已由指纹比对确认过。"
    if node.issue == "new":
        return "基线建立之后新增的文件，属正常变动；下次复检会自动纳入比对。"
    return ""


# --------------------------------------------------------------------------- #
# 两次运行的对比：本次新出现的问题 / 上次有但这次没有了的
# --------------------------------------------------------------------------- #

@dataclass
class Change:
    """两次运行之间的问题变化。kind: new=本次新出现, fixed=本次不再有问题。"""

    path: str
    issue: str
    previous_issue: str = ""
    kind: str = "new"

    @property
    def issue_text(self) -> str:
        if self.kind == "new" and self.previous_issue:
            return f"{CAT_TEXT.get(self.previous_issue, self.previous_issue)} → {CAT_TEXT.get(self.issue, self.issue)}"
        return CAT_TEXT.get(self.issue, self.issue)

    @property
    def severity(self) -> int:
        return SEVERITY.get(self.issue, 0)


def problem_map(root: DirNode | None) -> dict[str, str]:
    """path -> issue，只收有问题（issue 非空）的文件。"""
    out: dict[str, str] = {}
    if root is None:
        return out

    def walk(node: DirNode) -> None:
        for child in node.dirs.values():
            walk(child)
        for fnode in node.files.values():
            if fnode.issue:
                out[fnode.path] = fnode.issue

    walk(root)
    return out


def diff_problems(old: DirNode | None, new: DirNode | None) -> list[Change]:
    """对比两次运行的问题集合。

    同一个路径上问题类型变严重（例如"时间不同"变成"内容不同"）也算新问题，
    因为这是用户最需要看到的升级信号。
    """
    before = problem_map(old)
    after = problem_map(new)
    changes: list[Change] = []
    for path, issue in after.items():
        previous = before.get(path)
        if previous is None:
            changes.append(Change(path, issue, "", "new"))
        elif previous != issue:
            changes.append(Change(path, issue, previous, "new"))
    for path, issue in before.items():
        if path not in after:
            changes.append(Change(path, issue, "", "fixed"))
    changes.sort(key=lambda c: (c.kind != "new", -c.severity, c.path))
    return changes
