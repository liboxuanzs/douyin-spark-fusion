#!/usr/bin/env python3
"""浏览器探针 —— 部署后第一时间确认"这台机器真的能起浏览器"。

只读：打开空白页、算一道算术、读一次 UA，然后关掉。不登录、不访问抖音。

    python tools/browser_probe.py            # 走 core.browser（含 cloakbrowser → playwright 回落）
    BROWSER_BACKEND=playwright python tools/browser_probe.py

成功输出 `BROWSER_PROBE_OK`，失败输出原因并以退出码 1 结束 ——
部署脚本靠这两个信号决定是否继续。
"""
import os
import sys
from pathlib import Path

# 以**文件位置**回推仓库根：以前 sys.path 与 `.env` 都跟着 CWD 走，在 /opt 下敲
# `python tools/browser_probe.py` 会去读 /opt/.env（或干脆没有），配置静默跑偏。
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

if (_ROOT / ".env").exists():
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")


def main():
    from core.browser import get_browser

    browser = None
    try:
        browser = get_browser()
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto("about:blank")
        ua = page.evaluate("() => navigator.userAgent")
        n = page.evaluate("() => 1 + 1")
        assert n == 2
        print(f"BROWSER_PROBE_OK backend={os.environ.get('BROWSER_BACKEND', 'auto')}")
        print(f"ua={ua}")
        ctx.close()
        return 0
    except Exception as e:                      # noqa: BLE001 —— 部署脚本要看原因
        print(f"BROWSER_PROBE_FAILED {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
