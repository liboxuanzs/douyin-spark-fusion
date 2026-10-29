# -*- coding: utf-8 -*-
"""发送内容预览（issue #17）：面板上"这一轮到底会发出去什么"。

面板原来只显示模板原文（`[API]` 原样、字面 `\n` 原样），存完只说"单独配了内容（3 行）"。
可用户真正需要知道的是**好友手机里会收到几行、多少字、哪些字会被静默吃掉** ——
`[API]` 每个好友各取一条一言、取不到时整行删除（#5 的修复），面板却一直说 3 行。

这个模块是**纯函数、绝不联网**：一言位置用一条示例文案顶上，并说明"示例"二字。
调用它的地方只有面板（`webui/viewmodel.py`）；真正发送仍走 `core/msg_builder.render_message`。

预览的几条口径都对齐 `core/douyin_im.line_list()` / `split_message_lines()`：

1. 字面 `\r\n`/`\n` 与真换行都算换行（手写 `.env` 与面板提交两种形态都有）。
2. 零宽字符与两侧空白会被删掉；只有空白的行整行丢掉。
3. 行数与字数按**丢掉空行之后**算，否则"3 行"里可能有一行是假的。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

__all__ = ["preview", "normalize_template", "ZERO_WIDTH_CHARS", "SAMPLE_LINE"]

# 与 core/douyin_im.ZERO_WIDTH_CHARS 必须逐字一致（tests/test_message_preview.py 钉住）。
ZERO_WIDTH_CHARS = "\u200b\u200c\u200d\u2060\ufeff"

# 一言的占位符，以及"写得像但其实不是"的几种常见形态（只认大写方括号）。
API_TOKEN = "[API]"
_API_LOOKALIKE = re.compile(r"\[\s*api\s*\]|【\s*[Aa][Pp][Ii]\s*】", re.IGNORECASE)

SAMPLE_LINE = "今天也要开心呀"

# 抖音输入框一行能塞多少字没人说得准，但超过这个量基本会被截断或分条。
LINE_SOFT_LIMIT = 200
MESSAGE_SOFT_LIMIT = 500


def _strip_invisible(line: str) -> str:
    """删掉零宽字符与两侧空白（与 core/douyin_im 的同名函数同义）。"""
    text = str(line or "")
    for ch in ZERO_WIDTH_CHARS:
        text = text.replace(ch, "")
    return text.strip()


def normalize_template(template: Any) -> str:
    """把 `.env` 里的模板归一成"带真换行的文本"（不联网、不替换占位符）。

    顺序照抄 `core/douyin_im.split_message_lines()`：先收字面 `\r\n`，再收字面 `\n`，
    再收真 CRLF 与裸 `\r`。两条规则作用在互不相交的字符集上，顺序只是为了读起来顺。
    """
    text = str(template or "")
    if not text:
        return ""
    return (
        text.replace("\\r\\n", "\\n")
        .replace("\\n", "\n")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )


def _warn(code: str, level: str, text: str) -> Dict[str, str]:
    return {"code": code, "level": level, "text": text}


def preview(template: Any, *, sample: str = SAMPLE_LINE) -> Dict[str, Any]:
    """算出一份可直接渲染的预览。

    `sample` 是一言位置的替身：界面要明确标出"这是示例"，不能让用户以为那就是
    好友会收到的原话。
    """
    raw = str(template or "")
    text = normalize_template(raw)
    sample_text = str(sample or SAMPLE_LINE).strip() or SAMPLE_LINE

    warnings: List[Dict[str, str]] = []
    lines: List[Dict[str, Any]] = []
    blank_lines = 0
    zero_width_lines = 0
    padded_lines = 0
    lookalike_lines = 0
    has_api = False

    for raw_line in text.split("\n"):
        if any(ch in raw_line for ch in ZERO_WIDTH_CHARS):
            zero_width_lines += 1
        visible = raw_line
        for ch in ZERO_WIDTH_CHARS:
            visible = visible.replace(ch, "")
        stripped = visible.strip()
        if not stripped:
            blank_lines += 1
            continue
        # 只看空格：零宽字符已经单独记过一次，别让同一行报两条。
        if visible != stripped:
            padded_lines += 1
        if _API_LOOKALIKE.search(stripped) and API_TOKEN not in stripped:
            lookalike_lines += 1
        used_sample = API_TOKEN in stripped
        if used_sample:
            has_api = True
        rendered = stripped.replace(API_TOKEN, sample_text)
        lines.append({"text": rendered, "sample": used_sample})

    chars = sum(len(line["text"].replace(sample_text, "")) for line in lines)
    chars_with_sample = sum(len(line["text"]) for line in lines)

    if zero_width_lines:
        warnings.append(_warn(
            "zero_width", "info",
            f"有 {zero_width_lines} 行含零宽字符（肉眼看不见），发送前会被删掉。",
        ))
    if padded_lines:
        warnings.append(_warn(
            "padded", "info",
            f"有 {padded_lines} 行首尾带空格，发送时会被去掉。",
        ))
    if blank_lines:
        warnings.append(_warn(
            "blank_line", "info",
            f"有 {blank_lines} 行只有空白，发送时整行丢掉（不计入行数）。",
        ))
    if lookalike_lines:
        warnings.append(_warn(
            "api_case", "warn",
            f"有 {lookalike_lines} 行写了 [api]/【API】这类占位符 —— 只认大写方括号 [API]，"
            "现在它会原样发给好友。",
        ))
    if has_api and len(lines) == 1 and lines[0]["text"] == sample_text:
        warnings.append(_warn(
            "api_only", "warn",
            "整条只有 [API]：一言取不到时这条会变成空消息，那一轮整条不发。",
        ))
    if not lines:
        warnings.append(_warn(
            "empty", "warn",
            "没有可发送的内容：真的跑起来时这一轮会记输入失败，什么都不发。",
        ))
    long_lines = [
        (number, len(line["text"]))
        for number, line in enumerate(lines, start=1)
        if len(line["text"]) > LINE_SOFT_LIMIT
    ]
    if long_lines:
        number, length = long_lines[0]
        more = f"（另有 {len(long_lines) - 1} 行也超）" if len(long_lines) > 1 else ""
        warnings.append(_warn(
            "line_long", "warn",
            f"预览第 {number} 行 {length} 字，超过 {LINE_SOFT_LIMIT} 字{more}，"
            "输入框可能截断或分条。",
        ))
    if chars_with_sample > MESSAGE_SOFT_LIMIT:
        warnings.append(_warn(
            "message_long", "warn",
            f"整条约 {chars_with_sample} 字，超过 {MESSAGE_SOFT_LIMIT} 字，"
            "好友那边可能显示不全。",
        ))

    if lines:
        summary = f"{len(lines)} 行 · {chars} 字"
        if has_api:
            summary += f"（含示例一言 {chars_with_sample} 字）"
    else:
        summary = "0 行 · 没有内容"

    return {
        "ok": bool(lines),
        "template": raw,
        "normalized": text,
        "lines": lines,
        "line_count": len(lines),
        "chars": chars,
        "chars_with_sample": chars_with_sample,
        "has_api": has_api,
        "sample": sample_text,
        "blank_lines": blank_lines,
        "warnings": warnings,
        "has_warnings": bool(warnings),
        "summary": summary,
    }
