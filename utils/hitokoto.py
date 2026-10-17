import re

import requests

from utils.config import get_config
from utils.logger import setup_logger

logger = setup_logger("hitokoto")

hitokotoApi = "https://v1.hitokoto.cn/"

allHitokotoTypes = {
    "动画": "a",
    "漫画": "b",
    "游戏": "c",
    "文学": "d",
    "原创": "e",
    "来自网络": "f",
    "其他": "g",
    "影视": "h",
    "诗词": "i",
    "哲学": "k",
    "抖机灵": "l",
}


def request_hitokoto():
    """请求一言 API 获取一句话"""
    config = get_config()
    
    api_url = hitokotoApi

    for t in allHitokotoTypes.keys():
        if t in config["hitokotoTypes"]:
            if "?" not in api_url:
                api_url += "?"
            if "c=" in api_url:
                api_url += f"&c={allHitokotoTypes[t]}"
            else:
                api_url += f"c={allHitokotoTypes[t]}"

    try:
        response = requests.get(api_url, timeout=10)
        response.raise_for_status()
        data = response.json()
        theFrom = data.get("from")
        if theFrom is None or theFrom.strip() == "":
            theFrom = "未知来源"
        theFromWho = data.get("from_who")
        if theFromWho is None or theFromWho.strip() == "":
            theFromWho = "未知作者"
        # 一言正文/来源/作者都可能带换行，直接拼进模板会把行结构拆散
        # （模板靠 \n 分行发送）。这里统一拍平成单行：连续空白收成一个空格。
        # 用 .get 而不是下标：缺字段时原样抛 KeyError 会被下面的 except 吞掉，
        # 退化成"[error] 无法获取一言内容"，问题被掩盖。
        # 先取原值再转字符串：`str(None)` 是 "None" 这个**非空**字符串，
        # 直接 str(data.get(...)) 会把 null 正文变成 "None —— 未知来源"。
        raw_quote = data.get("hitokoto")
        quote = re.sub(r"\s+", " ", str(raw_quote or "")).strip()
        if not quote:
            # 正文缺失（接口改版 / 返回 null）也按取不到处理：否则会发出
            # "None —— 未知来源 (未知作者)" 这种东西。
            logger.warning("[一言] 接口没返回正文，按取不到处理")
            return ""
        theFrom = re.sub(r"\s+", " ", theFrom).strip()
        theFromWho = re.sub(r"\s+", " ", theFromWho).strip()
        return f"{quote} —— {theFrom} ({theFromWho})"
    except Exception as exc:
        # ★ 绝不能返回一句「看起来像正文」的字符串：调用方
        # `core/msg_builder.render_message` 会把它当消息内容真的发出去，
        # 对方收到的是"[error] 无法获取一言内容"。返回空串，由调用方决定
        # （去掉这一行；模板里只剩占位符时宁可拒发）。
        logger.warning("[一言] 获取失败："
                       f"{type(exc).__name__}: "
                       f"{' '.join(str(exc).split())[:120]}")
        return ""
