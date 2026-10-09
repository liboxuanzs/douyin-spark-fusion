"""日志尾部读取 —— **只读文件末尾**，绝不把整个日志读进内存。

日志文件在生产环境可能长到几个 GB，所以：

1. ``seek(0, os.SEEK_END)`` 之后**从后往前**分块读（默认 64KB 一块）；
2. 读够 N+1 个换行就停，整体字节上限默认 512KB（``max_bytes``）；
3. 文件在读取过程中被轮转/截断（``OSError``）时返回已经拿到的部分，不抛异常；
4. 单行过长时退化成"只显示这一行"，不返回空。

行数由查询参数 ``lines`` 控制（默认 300，上限 2000），clamp 在 ``app.py``/``__main__`` 里做。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

__all__ = ["DEFAULT_CHUNK", "DEFAULT_LINES", "MAX_LINES", "MAX_TAIL_BYTES", "LogTail", "read_tail"]

DEFAULT_LINES = 300
MAX_LINES = 2000
DEFAULT_CHUNK = 64 * 1024
MAX_TAIL_BYTES = 512 * 1024


@dataclass
class LogTail:
    """日志尾部读取结果。"""

    path: str = ""
    requested: int = DEFAULT_LINES
    lines: List[str] = field(default_factory=list)
    exists: bool = False
    bytes_read: int = 0
    truncated: bool = False
    error: str = ""

    @property
    def count(self) -> int:
        return len(self.lines)

    def as_text(self, limit: Optional[int] = None) -> str:
        rows = self.lines if limit is None else self.lines[-limit:]
        return "\n".join(rows)


def clamp_lines(value, default: int = DEFAULT_LINES, maximum: int = MAX_LINES) -> int:
    """把 ``lines`` 查询参数 clamp 到 [1, maximum]；非法值回落到默认。

    两种"看起来都是零"的输入要分开处理：

    * ``0`` —— 用户真的填了 0，但"显示 0 行"没有意义，抬到 1（至少给一行）；
    * 负数 / 非数字 —— 属于乱填，按默认值 300 处理（以前负数被一起塞进 1，
      于是 ``lines=-5`` 只会显示一行日志，页面上看不出是参数错了）。
    """
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < 0:
        return default
    if parsed == 0:
        return 1
    return min(parsed, maximum)


def _read_newlines_backwards(path: Path, wanted: int, max_bytes: int, chunk: int) -> Tuple[bytes, bool]:
    """从尾部往前读，凑够 ``wanted + 1`` 个换行为止；返回 ``(字节块, 是否被截断)``。"""
    with open(path, "rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        if size == 0:
            return b"", False
        limit = max(int(max_bytes), 64 * 1024)
        position = size
        pieces: List[bytes] = []
        collected = 0
        newlines = 0
        while position > 0 and collected < limit:
            step = min(chunk, position, limit - collected)
            if step <= 0:
                break
            position -= step
            handle.seek(position)
            pieces.append(handle.read(step))
            collected += step
            newlines += pieces[-1].count(b"\n")
            # 已经有 wanted+1 个换行，说明最后 wanted 行都拿全了（本块可能只含其中一部分换行，
            # 但累计值已经是下界，够用）。
            if newlines >= wanted + 1:
                break
        blob = b"".join(reversed(pieces))
        return blob, position > 0


def read_tail(
    path: Path,
    lines: int = DEFAULT_LINES,
    *,
    max_bytes: int = MAX_TAIL_BYTES,
    chunk: int = DEFAULT_CHUNK,
) -> LogTail:
    """读日志最后 ``lines`` 行。文件不存在也返回结果对象（``exists=False``）。"""
    requested = clamp_lines(lines)
    target = Path(path)
    result = LogTail(path=str(target), requested=requested)

    if not target.exists():
        result.error = "日志文件不存在"
        return result

    result.exists = True
    try:
        blob, truncated = _read_newlines_backwards(target, requested, max_bytes, chunk)
    except OSError as exc:
        result.error = f"读取日志失败：{type(exc).__name__}: {exc}"
        return result

    result.bytes_read = len(blob)
    result.truncated = truncated
    text = blob.decode("utf-8", "replace")
    rows = text.split("\n")

    # 文件通常以换行结尾 → 末尾会多出一个空元素。
    if rows and rows[-1] == "":
        rows = rows[:-1]

    # 中间可能有 \r\n（Windows 上跑过），统一去掉尾部 \r。
    rows = [row[:-1] if row.endswith("\r") else row for row in rows]

    if not rows and text:
        rows = [text]
    result.lines = rows[-requested:] if requested > 0 else rows
    return result


def read_tail_with_fallback(
    candidates: List[Path],
    lines: int = DEFAULT_LINES,
    *,
    max_bytes: int = MAX_TAIL_BYTES,
) -> LogTail:
    """按候选顺序找第一个存在的日志文件；都不存在时报告首选路径。"""
    first: Optional[LogTail] = None
    for candidate in candidates:
        tail = read_tail(candidate, lines, max_bytes=max_bytes)
        if first is None:
            first = tail
        if tail.exists and tail.lines:
            return tail
    return first if first is not None else LogTail(requested=clamp_lines(lines))
