"""core/douyin_im.py
抖音网页版 IM 会话扫描库 —— 传一个 page，内部自己开门、自己滚、自己找人。

设计契约（三句话）
    1. `DouyinIM(page)` 内部立即挂只读监听钩子，然后才 goto 打开聊天页。
       顺序不可颠倒：钩子必须早于导航，否则首屏响应会漏。
    2. `on_ready(cb)` 注册就绪回调。库自己完成「登录校验 + 会话列表就绪」两道门禁，
       终态通过 status 区分，结果对象同一形状（见 ReadyResult）。
    3. `iter_find_and_select(targets)` 从头滚到尾逐个找人。
       **yield 出来的那一刻，该会话已经被选中且 conv_id 已校验过** ——
       调用方拿到就能直接输入、发送，不需要再定位。

不做什么
    - 不模拟人类操作。cloakbrowser 以 `humanize=True` 启动时已自带拟人化，
      本库只负责「点对地方」，不重复加随机延迟。
    - 不自己构造任何请求。全程只读监听，不签名、不重放。
    - 不假设会话列表非虚拟化。列表是虚拟化的（同时渲染约 7-10 个窗口），
      因此采集必须边滚边累积，且「到底」判定不能用条目数。

本文件是**自足**的：解析层（选择器 / 页面内表达式 / protobuf 解码 / SSR 抓取 /
只读监听器 / 登录态判定 / 身份算法）全部内联在此
本文件结构：
    一、选择器          二、页面内表达式      三、protobuf 解码
    四、SSR 登录态      五、身份算法          六、只读监听器 ImMonitor
    七、登录态判定      八、DouyinIM 主体
"""

import time
import traceback
import json
import re
import unicodedata
from urllib.parse import unquote

from utils.config import get_config
from utils.logger import setup_logger
from core import util_time

# 本模块的日志器。名字固定 "douyin_im"，与 core.tasks 的 "app" 分开，
# 便于单独按 logger 名过滤。级别取自 .env 的 LOG_LEVEL（utils.config 统一读）。
#
# 注意：这里在**模块导入时**就初始化。utils.config 是纯 env 读取、无网络/无 IO，
# 所以 import 期调用是安全的（它自己的 config 也是这么读的）。
logger = setup_logger("douyin_im", level=get_config().get("logLevel", "Info"))

# 单次调用超过这么多秒就告警（Playwright 默认超时 120s，静默等到那时日志会一片空白）
SLOW_CALL_SECONDS = 3.0

# 严格确认（fail-closed）判定层。core.strict_verify 在**模块级**反过来 import
# 本文件的 `_is_group` / `_norm` / `peer_uid_of`，所以这里只能**函数内惰性导入**，
# 否则 import 环。写成模块级小包装，调用点读起来干净。
_strict_verify_mod = None


def strict_verify():
    """惰性拿到 core.strict_verify 模块（见上方注释）。"""
    global _strict_verify_mod
    if _strict_verify_mod is None:
        from core import strict_verify as _sv
        _strict_verify_mod = _sv
    return _strict_verify_mod


# ---------------------------------------------------------------------------
# dry-run 分级（档位常量与解析规则在 `core/util_time.py`，全项目只有那一份）。
# 空跑的**唯一目的**是让人在不真的发消息的前提下，验证
# "扫描 → 严格确认 → 输入"这条链路的每一环。
# ---------------------------------------------------------------------------

DRY_RUN_OFF = util_time.DRY_RUN_OFF
DRY_RUN_SELECT = util_time.DRY_RUN_SELECT
DRY_RUN_FULL = util_time.DRY_RUN_FULL
DRY_RUN_LEVELS = util_time.DRY_RUN_LEVELS
norm_dry_run = util_time.norm_dry_run


# 不可见填充字符：Draft.js 在软换行（`<br>`）与空块里塞 ZERO WIDTH SPACE 之类的
# 零宽字符来撑住块结构。它们**肉眼看不见、`str.strip()` 与 JS `trim()` 也删不掉**
# （`'\u200b'.isspace()` 是 False），于是一份"看起来完全正确"的读回文本会被判成
# 不一致 —— 2026-10-03 第一次真机 dry-run=full 就是这么误报的。比文本前先统一删掉。
ZERO_WIDTH_CHARS = "\u200b\u200c\u200d\u2060\ufeff"


def _strip_invisible(line: str) -> str:
    """删掉零宽字符与两侧空白。"""
    text = str(line or "")
    for ch in ZERO_WIDTH_CHARS:
        text = text.replace(ch, "")
    return text.strip()


def line_list(text):
    """把一段文本切成"非空行"（删掉零宽字符与两侧空白，空行丢掉）。"""
    return [line for line in (_strip_invisible(part)
                              for part in str(text or "").replace("\r\n", "\n").split("\n"))
            if line]


def _readback_matches(expected_lines, got_text):
    """编辑器读回的内容是否等于"应该输入的行" → (是否匹配, 实际读回的原文)。

    ★ **按行比对，不能只比字符**：老实现两边都 `re.sub(r"\\s+", "", ...)`，
    于是 `"甲\\n乙"` 与 `"甲乙"` 归一化后相等 —— 换行丢了也判"匹配"，
    `dry-run=full` 这一档就白跑了（它的全部价值就是证明"文本真的进了编辑器"，
    包括行结构）。现在：行数一样、每行文字一样才算过；少一行/多一行都算不匹配。

    行内空白仍然忽略（Draft.js 的 textContent/innerText 会在块边界补换行、
    行首行尾也可能多出空白），空行忽略；Draft.js 塞的**零宽字符**（软换行处
    的 `\\u200b` 等，肉眼与 `trim()` 都看不见）同样忽略，见 `ZERO_WIDTH_CHARS`。
    """
    want = line_list("\n".join(expected_lines or []))
    got = line_list(got_text)
    return want == got, (got_text or "")


def _squash(text) -> str:
    """压成"只剩可见字符"的一串（删掉所有空白），用来数同一个片段出现了几次。"""
    return "".join(str(text or "").split())


def _bare(seg: str) -> str:
    """去掉一段两端的**装饰性**字符（emoji / 符号 / 标点）—— `🌈lbx🌈` → `lbx`。

    给子串兜底用：好友昵称上挂 emoji 是常态（本项目自己的号就叫 `🌈LBX🌈`），
    手写的目标名不会带装饰。只削两端，中间的字符一个不动，所以
    `我是小明明` 削完还是 `我是小明明`，不会退化成任意子串匹配。
    """
    i, j = 0, len(seg)
    while i < j and not seg[i].isalnum():
        i += 1
    while j > i and not seg[j - 1].isalnum():
        j -= 1
    return seg[i:j]


def _residue_echo(expected_lines, got_text):
    """读回里是否出现"同一条消息两遍" → (是否叠加, 首行出现次数)。

    ★ 只在**有强证据**时才判叠加：数首行在读回里出现了几次，**比这条消息自己
    就该有的次数还多**才算残留 —— 上一条的旧文本在重渲染里回来了、新文本又接在
    后面，发出去朋友会看到两遍。
    刻意**不拿"读回 ≠ 期望"当叠发判据**：那种差异可能只是排版差别，而在真跑里
    误判的代价是整天不发（比叠发更亏）。

    ★ 为什么是"比期望多"而不是">= 2"：读回的就是**这一条正要发的消息**，它的
    首行本来就出现了一次以上。模板自己重复了首行（`MESSAGE_TEMPLATE` 里两行
    一样、或 `[API]` 抽到的句子恰好等于模板首行）时，`>= 2` 会把"正常文本"判成
    "有残留"，这条就**每轮都发不出去**，日志还指着一个不存在的清空 bug。
    """
    wanted = line_list("\n".join(expected_lines or []))
    head = _squash((wanted or [""])[0])
    body = _squash(got_text)
    # 太短的首行（"hi" 这种）到处都会撞上，不作数 —— 宁可不拦，也不误拦
    if len(head) < 4 or not body:
        return False, 0
    # 这条消息自己会让首行出现几次（模板重复首行时 >1），超出它才算真残留
    due = _squash("".join(wanted)).count(head)
    n = body.count(head)
    return n > due, n


def _brief(exc) -> str:
    """异常压成一行（Playwright 的异常常带多行 Call log）。"""
    return f"{type(exc).__name__}: {' '.join(str(exc).split())}"


def _slow_warn(label: str, elapsed: float) -> None:
    """慢调用告警。成功返回的慢调用同样要报。"""
    if elapsed >= SLOW_CALL_SECONDS:
        logger.warning(f"[SLOW] {label} 耗时 {elapsed:.1f}s")


CHAT_URL = "https://www.douyin.com/chat"

# 会话项行高（CSS 实测 height:67px），用于按 data-index 估算 scrollTop
ROW_HEIGHT = 67

# 会话列表的独立容器。主容器之外还有折叠组与陌生人组，
# 它们各自是独立滚动容器，主容器扫不到 —— 见 DouyinIM.fold_groups。
SEL_LIST_FOLD = ".conversationFoldConversationListlistWrapper"
SEL_LIST_STRANGER = ".conversationStrangerConversationListlistWrapper"

# ===========================================================================
# 一、选择器（全部来自 HAR 里 pcim 打包产物的 CSS/JS 原文）
# ===========================================================================

SEL_ITEM = '[data-e2e="conversation-item"]'          # 会话项官方挂点
SEL_TITLE = ".conversationConversationItemtitle"     # 会话显示名（备注优先）
SEL_LIST = ".conversationConversationListwrapper"    # 列表滚动容器
SEL_CUR = ".conversationConversationItemcurConversation"  # 当前选中态
SEL_LOGIN_BOX = '[data-e2e="login-container"]'       # 未登录时渲染
SEL_AVATAR = '[data-e2e="user-avatar-card"]'         # 登录后头像卡
SEL_MSG_ITEM = '[data-e2e="msg-item-content"]'
SEL_MSG_FROM_ME = ".MessageBoxContentisFromMe"
SEL_SEND_BTN = ".messageMsgInputpublishBtn"
SEL_SEND_BTN_READY = ".messageMsgInputpublishBtn.messageMsgInputpublishRedBtn"  # 有内容可发

# 输入框：优先 contenteditable 本体（humanize 的"可编辑"检查能过），容器兜底
EDITOR_CANDIDATES = (
    '[data-e2e="msg-input"] .public-DraftEditor-content',
    '.DraftEditor-root [contenteditable="true"]',
    '.messageEditorimChatEditorContainer',
)

# ===========================================================================
# 二、页面内执行表达式（page.evaluate 直接跑，返回结构化结果）
# ===========================================================================

JS_LOGIN_DOM = """(() => {
  const shown = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.display !== 'none'
        && st.visibility !== 'hidden' && parseFloat(st.opacity || '1') > 0.05;
  };
  const box = document.querySelector('[data-e2e="login-container"]');
  const boxVisible = shown(box);
  // 2026-10 新版登录弹窗没有 data-e2e，只有 class / 组件 id。
  // 线上 wheat 掉登录那天：页面上明摆着画着这个弹窗，旧选择器一个都没命中，
  // 于是门禁判成"等待超时"，用户看到的是"超时"而不是"去重新登录"。
  const fresh = document.querySelector('.douyin_login_new_class')
             || document.querySelector('#douyin_login_comp_mobile_code')
             || document.querySelector('#douyin_login_comp_button_input_id');
  const hasChatRoot = !!document.querySelector('[data-e2e="msg-input"]')
                   || !!document.querySelector('.conversationConversationListwrapper');
  const body = (document.body && document.body.innerText) || '';
  const loginWords = /扫码登录|验证码登录|密码登录/.test(body);
  return {
    loginVisible: boxVisible,     // 登录框**真的画在屏幕上**（不止是存在于 DOM）
    loginBox: !!box,
    // 新版弹窗真的画出来了；或者整页**连聊天界面都没有**却写着登录用词（兜底）
    loginNew: shown(fresh) || (loginWords && !hasChatRoot),
    loginWords: loginWords,
    avatarCard: !!document.querySelector('[data-e2e="user-avatar-card"]'),
    hasChatRoot: hasChatRoot,
    itemCount: document.querySelectorAll('[data-e2e="conversation-item"]').length,
  };
})()"""

# 列表"可以开始滚了"（≠ 列表已全量加载）。标题非空、头像已加载、容器有高度。
JS_LIST_READY = """(() => {
  const items = document.querySelectorAll('[data-e2e="conversation-item"]');
  if (items.length === 0) return { ready: false, why: 'no-item', count: 0 };
  let blank = 0;
  items.forEach(el => {
    const t = el.querySelector('.conversationConversationItemtitle');
    if (!t || !t.textContent.trim()) blank++;
  });
  if (blank > 0) return { ready: false, why: 'title-blank', blank, count: items.length };
  const img = items[0].querySelector('img');
  if (img && !img.complete) return { ready: false, why: 'avatar-loading', count: items.length };
  const box = document.querySelector('.conversationConversationListwrapper');
  if (!box || box.clientHeight <= 0) return { ready: false, why: 'no-container', count: items.length };
  return { ready: true, count: items.length };
})()"""

# 采集当前窗口的会话：DOM 字段 + React fiber 里的 conversation 模型。
# 注意：虚拟列表下这里只返回**当前窗口那 7-10 项**，不是全部好友。
JS_COLLECT = """(() => {
  const ITEM = '[data-e2e="conversation-item"]';

  function convOf(el) {
    const keys = Object.keys(el).filter(k => k.startsWith('__reactProps$') || k.startsWith('__reactFiber$'));
    for (const k of keys) {
      if (k.startsWith('__reactProps$')) {
        const p = el[k];
        if (p && p.conversation && (p.conversation.id || p.conversation.conversationId)) return p.conversation;
        continue;
      }
      let f = el[k], d = 0;
      while (f && d++ < 30) {
        const p = f.memoizedProps;
        if (p && p.conversation && (p.conversation.id || p.conversation.conversationId)) return p.conversation;
        f = f.return;
      }
    }
    return null;
  }
  // 标题里的空格被前端 TextKeepSpaces 换成了 U+00A0（源码明文 replace(/ /g,"\\xa0")），
  // 不还原的话标题会带隐形字符，落库/比较都容易踩坑。
  const clean = s => s == null ? null : String(s).replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
  const txt = (el, s) => { const n = el.querySelector(s); return n ? clean(n.textContent) : null; };
  const has = (el, s) => !!el.querySelector(s);
  const safe = fn => { try { return fn(); } catch (e) { return null; } };

  return [...document.querySelectorAll(ITEM)].map((el, index) => {
    const conv = convOf(el);
    const convId = conv ? (conv.id || conv.conversationId || null) : null;
    // toParticipantUserId / toParticipantSecUserId 是 prototype getter（内部拿 this.ctx 比对自己 uid），
    // 包装一层 try：会话被折叠/退出时 getter 可能抛错，不能让整个采集挂掉。
    const uidRaw = conv ? safe(() => conv.toParticipantUserId ?? null) : null;
    // 虚拟列表 item 容器上的 data-index（库代码 jsx("div",{"data-index":e.index})）。
    // 它是「渲染序号」不是身份，重排/虚拟化会变，只作调试与交叉验证用。
    const holder = el.closest('[data-index]');
    return {
      index,
      dataIndex: holder ? Number(holder.getAttribute('data-index')) : null,
      convId,
      type: conv ? (conv.type ?? null) : null,
      uid: (uidRaw === null || uidRaw === undefined) ? null : String(uidRaw),
      secUid: conv ? (conv.toParticipantSecUserId ?? null) : null,
      shortId: conv ? (conv.shortId ?? conv.short_id ?? null) : null,
      participantCount: conv ? (conv.participantCount ?? null) : null,
      title: txt(el, '.conversationConversationItemtitle'),
      desc: txt(el, '.ConversationItemDescwrapper'),
      unread: txt(el, '.ConversationItemUnReadCountim-saas-unreadCountBadge'),
      isCurrent: has(el, '.conversationConversationItemcurConversation'),
      isMuted: has(el, '.ConversationItemTagNextToTitlemuteIcon'),
      isOnline: has(el, '.ConversationItemDesconlineStatus'),
      fromFiber: !!conv,
    };
  });
})()"""

# 滚动状态。三个量各管各的，别混：
#   atBottom  —— 用户是否滚到底（我们要的）
#   notFilled —— 内容还没填满容器（前端用它决定要不要继续补页，不是到底）
JS_SCROLL_PROBE = """(() => {
  const el = document.querySelector('.conversationConversationListwrapper');
  if (!el) return { found: false };
  return {
    found: true,
    scrollTop: Math.round(el.scrollTop),
    scrollHeight: el.scrollHeight,
    clientHeight: el.clientHeight,
    atBottom: el.scrollTop + el.clientHeight >= el.scrollHeight - 1,
    notFilled: el.scrollHeight <= el.clientHeight + 1,
    count: document.querySelectorAll('[data-e2e="conversation-item"]').length,
  };
})()"""

# 按绝对像素滚动。虚拟列表用 scrollTo 比 scrollBy 可控：
# 外部在 yield 期间可能改变布局，相对位移会累积误差。
JS_SCROLL_TO = """(top) => {
  const el = document.querySelector('.conversationConversationListwrapper');
  if (!el) return false;
  el.scrollTo({ top: top, behavior: 'instant' });
  return true;
}"""

# 记录扫描进度用的最小指纹：只要 data-index 与标题
JS_WINDOW_FINGERPRINT = """(() => {
  const items = [...document.querySelectorAll('[data-e2e="conversation-item"]')];
  const idx = items.map(el => {
    const h = el.closest('[data-index]');
    return h ? Number(h.getAttribute('data-index')) : null;
  }).filter(v => v !== null);
  return { count: items.length, minIndex: idx.length ? Math.min(...idx) : null,
           maxIndex: idx.length ? Math.max(...idx) : null };
})()"""

# 编辑器取值/操作统一用这一组选择器（顺序即优先级）：
# 读到内容的、判断空不空的、清空的、往里打字的，**必须是同一个节点** ——
# 否则会出现"读到的是 A、清的是 B"，判定全乱。
JS_EDITOR_SELECTORS = """  const pick = () => document.querySelector('[data-e2e="msg-input"] .public-DraftEditor-content')
          || document.querySelector('.DraftEditor-root [contenteditable="true"]')
          || document.querySelector('.messageMsgInput [contenteditable="true"]')
          || document.querySelector('[data-e2e="msg-input"] [contenteditable="true"]')
          || document.querySelector('[contenteditable="true"]');"""

# 零宽字符：Draft.js 在软换行与空块里塞 \\u200b，肉眼看不见、trim() 也删不掉
# （"\\u200b".trim() 仍是 "\\u200b"）—— 不删掉的话"已清空"会被判成没清空。
JS_EDITOR_ZW = """  const ZW = /[\\u200b\\u200c\\u200d\\u2060\\ufeff]/g;"""

# 编辑器里的可见文本（排版感知）：软换行是 <br>、硬换行是兄弟块，textContent
# 两种都不补换行符，而 innerText 都补 —— 判空与判行数都得靠它。
JS_EDITOR_READ = """  const read = () => {
    const ed = pick();
    if (!ed) return '';
    const raw = (typeof ed.innerText === 'string' && ed.innerText) || ed.textContent || '';
    return raw.replace(ZW, '');
  };"""

JS_EDITOR_EMPTY = """(() => {
""" + JS_EDITOR_SELECTORS + "\n" + JS_EDITOR_ZW + """
  const ed = pick();
  if (!ed) return null;
  // ★ 零宽字符不算内容：Draft.js 会在软换行与空块里塞 \\u200b，肉眼看不见、
  //   trim() 也删不掉（"\\u200b".trim() 仍是 "\\u200b"）—— 不删掉的话，
  //   "已经清空"会被判成没清空，下一轮还会以为有草稿残留。
  //   判空用 innerText：软换行（<br>）之后跟着的 \\u200b 删掉后就是空行，
  //   textContent 则会把各行直接粘起来，分不出"真的空"和"只剩填充"。
  const t = (((typeof ed.innerText === 'string' && ed.innerText) || ed.textContent || '')
             .replace(ZW, ''));
  return t.trim().length === 0;
})()"""

# 清空编辑器。**这不是"清 DOM"那么简单**：Draft.js 的内容真相在它自己的
# EditorState 里，只把 DOM 抹掉不改 state，React 下一次重渲染就会把旧文本带
# 回来 —— 症状是"清空了（下一轮判空也说空），一打字旧内容又冒出来"，重试时
# 两条消息叠着发。execCommand('delete') 在真机上就是这样：DOM 看着空了、
# state 没变，随后 dry-run=full 的读回里前一条消息原封不动地又出现了一遍。
#
# 所以按"Draft 认哪条路"逐级试，每级都用**删掉零宽字符后的可见文本**自校验：
#   ① 全选 + 合成 Backspace —— 与软换行同一条路（React 的 onKeyDown 认合成
#      事件，实测 brk='key'），走 Draft 自己的删除逻辑，state 会跟着走；
#   ② 全选 + execCommand('delete') —— 老办法，兜底；
# 返回 {ok, how, left, tries}：调用方必须看 ok，而不是"我调用过清空"。
JS_EDITOR_CLEAR = """(() => {
""" + JS_EDITOR_SELECTORS + "\n" + JS_EDITOR_ZW + "\n" + JS_EDITOR_READ + """
  const selectAll = () => {
    const ed = pick();
    if (!ed) return null;
    try { ed.focus(); } catch (e) {}
    try {
      const r = document.createRange();
      r.selectNodeContents(ed);
      const s = window.getSelection();
      s.removeAllRanges();
      s.addRange(r);
    } catch (e) { return null; }
    return ed;
  };
  if (!pick()) return { ok: false, how: 'no-editor', left: '' };
  if (!read().trim()) return { ok: true, how: 'already-empty', left: '' };
  const tries = [];
  const note = (name, ok, extra) => {
    tries.push(Object.assign({ name: name, ok: ok }, extra || {}));
  };
  // ① 全选 + 合成 Backspace（Draft 自己的键盘路径）
  if (selectAll()) {
    for (const t of ['keydown', 'keyup']) {
      const ed = pick();
      if (ed) ed.dispatchEvent(new KeyboardEvent(t, {
        key: 'Backspace', code: 'Backspace', keyCode: 8, which: 8,
        bubbles: true, cancelable: true,
      }));
    }
    if (!read().trim()) { note('key', true); return { ok: true, how: 'key', left: '', tries: tries }; }
    note('key', false, { left: read().replace(/\\s+/g, ' ').trim().slice(0, 30) });
  }
  // ② 全选 + 浏览器原生 delete（兜底）
  if (selectAll()) {
    let ran = false;
    try { ran = document.execCommand('delete'); } catch (e) { ran = false; }
    if (!read().trim()) {
      note('cmd', true, { ran: ran });
      return { ok: true, how: 'cmd', left: '', tries: tries };
    }
    note('cmd', false, { ran: ran, left: read().replace(/\\s+/g, ' ').trim().slice(0, 30) });
  }
  return { ok: false, how: 'none', left: read().replace(/\\s+/g, ' ').trim().slice(0, 40),
           tries: tries };
})()"""

JS_CURRENT_CONV = """(() => {
  const el = document.querySelector('.conversationConversationItemcurConversation');
  if (!el) return null;
  const t = el.querySelector('.conversationConversationItemtitle');
  const title = t ? t.textContent.replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim() : null;
  const idx = [...document.querySelectorAll('[data-e2e="conversation-item"]')].indexOf(el);
  // safe()：fiber 上的 getter 可能抛（同一个 conv 在别处也有过这种坑），
  // 一个字段炸掉不能让整条读数失败 —— 那样校验会退化成"什么都没看到"。
  const safe = (fn) => { try { return fn(); } catch (e) { return null; } };
  let convId = null, peerUid = null;
  const keys = Object.keys(el).filter(k => k.startsWith('__reactFiber$'));
  for (const k of keys) {
    let f = el[k], d = 0;
    while (f && d++ < 30) {
      const p = f.memoizedProps;
      const c = p && (p.conversation || p.conv || p.item);
      if (c && (c.id || c.conversationId)) {
        convId = c.id || c.conversationId;
        // 对方 uid：与 conv_id 同源、同一条 fiber 上取。conv_id 读不到时它是
        // 唯一的硬证据，所以必须单独带出来（JS_COLLECT 用的也是这个 getter）。
        peerUid = safe(() => (c.participant && c.participant.toParticipantUserId)
                            || c.toParticipantUserId || null);
        break;
      }
      f = f.return;
    }
    if (convId || peerUid) break;
  }
  if (peerUid == null) peerUid = null;
  return { index: idx, title, convId: convId == null ? null : convId,
           peerUid: peerUid == null ? null : String(peerUid) };
})()"""

JS_SEL_DIAG = """((p) => {
  const items = [...document.querySelectorAll('[data-e2e="conversation-item"]')];
  let anyCur = 0, curSample = null;
  for (const e of document.querySelectorAll('*')) {
    const cn = e.className;
    if (typeof cn === 'string' && cn.indexOf('curConversation') >= 0) {
      anyCur++;
      if (!curSample) curSample = e.tagName + '|' + cn.slice(0, 80);
    }
  }
  let fromPoint = null;
  try {
    const px = (p && p.px) || 0, py = (p && p.py) || 0;
    const el = document.elementFromPoint(px, py);
    fromPoint = (el ? el.tagName + '#' + (el.getAttribute('data-e2e') || '') + '|' + String(el.className).slice(0, 60) : 'null') + ' @' + px + ',' + py;
  } catch (e) { fromPoint = 'err:' + e; }
  return {
    viewport: [window.innerWidth, window.innerHeight],
    dpr: window.devicePixelRatio,
    itemCount: items.length,
    curCount: document.querySelectorAll('.conversationConversationItemcurConversation').length,
    anyCurClass: anyCur,
    curSample: curSample,
    listBox: !!document.querySelector('[data-e2e="conversation-list"]'),
    msgInput: !!document.querySelector('[data-e2e="msg-input"]'),
    editor: !!document.querySelector('.public-DraftStyleDefault-block, [contenteditable="true"]'),
    chat: !!document.querySelector('[data-e2e="message-list"], .messageList, [data-e2e="chat-content"]'),
    fromPoint: fromPoint,
    titles: items.slice(0, 8).map(e => (e.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 18)),
  };
})"""

JS_EDITOR_EXISTS = """(() => {
  return !!(document.querySelector('[data-e2e="msg-input"] .public-DraftEditor-content')
         || document.querySelector('.DraftEditor-root [contenteditable="true"]')
         || document.querySelector('.messageMsgInput [contenteditable="true"]')
         || document.querySelector('[data-e2e="msg-input"] [contenteditable="true"]'));
})()"""

# 合成输入：**一次只处理一行**，由 Python 侧逐行调用、行间留出重渲染时间。
#
# 为什么不能在一个 JS 循环里插完：Draft.js 每收到一次 beforeinput 都会重渲染
# contenteditable，DOM 选区随之失效。连插多行时第 2 行起 execCommand 会静默
# no-op —— 症状就是「只发出去第一行，后面全是空行」。所以这里做成单行原语：
# 每次重新取节点、把光标强制移到末尾、再用 textContent 长度自校验插入是否真的发生。
#
# 换行同样是**自校验**的：合成 keydown 不一定被 Draft 认（React 只管真事件，
# 页面还可能拦），所以按 ①键盘事件 ②execCommand('insertLineBreak')
# ③直接插 '\n' 逐级退化，每一级都用"行数有没有变多"来验，结果放在 res.brk ——
# 'none' 就等于"换行没插进去"，Python 侧会告警、dry-run=full 会判不匹配。
JS_TYPE_LINE = """((p) => {
""" + JS_EDITOR_SELECTORS + """
  const caretToEnd = (ed) => {
    ed.focus();
    try {
      const r = document.createRange();
      r.selectNodeContents(ed);
      r.collapse(false);
      const s = window.getSelection();
      s.removeAllRanges();
      s.addRange(r);
    } catch (e) {}
    return ed;
  };
  // 行数（结构行）：innerText 会把 <br> 与块边界都算成换行，textContent 不会。
  // 先删零宽字符再切 —— Draft.js 的软换行后面跟着一个 \\u200b，它是"填充"不是
  // 文字；不删的话 `grew()` 会把"某个块多了一个零宽字符"误当成"多了一行"。
  // 这里刻意**不过滤空行**：软换行刚插进来时新行就是空的，过滤掉就看不见"行数变多"了。
""" + JS_EDITOR_ZW + """
  const lineCount = (ed) => {
    if (!ed) return 0;
    const t = (((typeof ed.innerText === 'string' && ed.innerText) || ed.textContent || '')).replace(ZW, '');
    return t.length ? t.split('\\n').length : 0;
  };
  const softBreak = (ed) => {
    const before = lineCount(ed);
    const grew = () => lineCount(pick() || ed) > before;
    for (const t of ['keydown', 'keyup']) {
      (pick() || ed).dispatchEvent(new KeyboardEvent(t, {
        key: 'Enter', code: 'Enter', keyCode: 13, which: 13,
        shiftKey: true, bubbles: true, cancelable: true,
      }));
    }
    if (grew()) return 'key';
    try {
      caretToEnd(pick() || ed);
      if (document.execCommand('insertLineBreak') && grew()) return 'cmd';
    } catch (e) {}
    try {
      caretToEnd(pick() || ed);
      document.execCommand('insertText', false, '\\n');
      if (grew()) return 'text';
    } catch (e) {}
    return 'none';
  };
  let ed = pick();
  if (!ed) return { err: 'no-editor' };
  const res = { ins: true, grew: 0, retry: false };
  if (p.text) {
    ed = caretToEnd(ed);
    const before = (ed.textContent || '').length;
    let ok = document.execCommand('insertText', false, p.text);
    let after = (ed.textContent || '').length;
    if (!ok || after <= before) {
      // 选区被重渲染吃掉时再补一次：重取节点 + 重设光标
      const e2 = caretToEnd(pick() || ed);
      ok = document.execCommand('insertText', false, p.text);
      after = (e2.textContent || '').length;
      res.retry = true;
    }
    res.ins = ok;
    res.grew = after - before;
  }
  if (p.enter) res.brk = softBreak(pick() || ed);
  res.lines = lineCount(pick() || ed);
  res.len = ((pick() || ed).textContent || '').length;
  return res;
})"""

JS_CLICK_SEND = """(() => {
  const b = document.querySelector('.messageMsgInputpublishBtn.messageMsgInputpublishRedBtn')
         || document.querySelector('[data-e2e="msg-send"]')
         || document.querySelector('.messageMsgInputpublishBtn');
  if (!b) return null;
  for (const t of ['mousedown', 'mouseup', 'click']) {
    b.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window }));
  }
  return 'js';
})()"""


JS_MSG_STATE = """(() => {
  const items = document.querySelectorAll('[data-e2e="msg-item-content"]');
  const last = items[items.length - 1];
  return {
    count: items.length,
    lastFromMe: last ? !!last.closest('.MessageBoxContentisFromMe') : null,
    lastText: last ? last.textContent.trim().slice(0, 80) : null,
  };
})()"""

# 读回输入框内容。dry-run=full 靠它证明"文本真的进了 Draft.js 编辑器"——
# 只看 Python 侧"我调用过 typing"是不够的（合成输入会静默缺行）。
#
# ★ 取 `innerText`，不取 `textContent`：Draft.js 的软换行渲染成 `<br>`、
#   硬换行渲染成兄弟块，这两种情况 `textContent` **都不会**补换行符
#   （`<div>甲<br>乙</div>`.textContent === "甲乙"），于是"换行丢了"永远验不出来。
#   `innerText` 是排版感知的，两种边界都会变成 `\n` —— 换行有没有真的进去，
#   只有它能证明。取不到 innerText 时（元素未渲染等）才退回 textContent，
#   并在返回值里标 fallback，由 Python 侧降级判断。
# ★ 返回值里顺手删掉零宽字符（`\u200b` 等）：它们是 Draft 的填充，肉眼看不见、
#   trim() 删不掉，留着只会让 Python 侧的比对把正确的文本判成不一致。
JS_EDITOR_TEXT = """(() => {
""" + JS_EDITOR_SELECTORS + "\n" + JS_EDITOR_ZW + """
  const ed = pick();
  if (!ed) return null;
  const hasInner = typeof ed.innerText === 'string';
  const raw = (hasInner && ed.innerText) || ed.textContent || '';
  const text = raw.replace(ZW, '');
  return {
    text: text,
    len: text.length,
    fallback: !hasInner,
    blocks: ed.querySelectorAll('.public-DraftStyleDefault-block').length,
    brs: ed.querySelectorAll('br').length,
  };
})()"""

# 限流/风控提示文案采集。
#
# 刻意**只扫"提示类"容器**（toast / dialog / alert / 弹层），不扫整页文本：
# 聊天内容里出现"验证码"三个字太正常了（好友自己就会聊到），扫整页会把
# 正常对话误判成熔断 —— 熔断要停一整轮，误报的代价很高。
#
# 双重护栏：① 容器里若含消息气泡或会话条目，整块跳过；
#           ② 单条文案超过 200 字直接丢（真提示语都很短）。
JS_LIMIT_TEXT = """(() => {
  const SEL = [
    '[role="alert"]', '[role="dialog"]', '[aria-live]',
    '[class*="toast"]', '[class*="Toast"]', '[class*="dialog"]', '[class*="Dialog"]',
    '[class*="modal"]', '[class*="Modal"]', '[class*="tips"]', '[class*="Tips"]',
    '[class*="notice"]', '[class*="Notice"]', '[class*="notify"]', '[class*="Notify"]',
    '[class*="captcha"]', '[class*="Captcha"]', '[class*="verify"]', '[class*="Verify"]',
  ].join(',');
  const visible = (el) => {
    try {
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) return false;
      const st = getComputedStyle(el);
      return st.visibility !== 'hidden' && st.display !== 'none'
             && Number(st.opacity || 1) > 0.05;
    } catch (e) { return false; }
  };
  const out = [];
  const seen = new Set();
  let nodes = [];
  try { nodes = [...document.querySelectorAll(SEL)]; } catch (e) { nodes = []; }
  for (const el of nodes) {
    if (out.length >= 20) break;
    if (!visible(el)) continue;
    // 护栏①：提示容器里不该有聊天内容/会话条目；有就说明这个选择器太宽了
    if (el.querySelector('[data-e2e="msg-item-content"], [data-e2e="conversation-item"]')) continue;
    const s = (el.textContent || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
    if (!s || s.length > 200 || seen.has(s)) continue;
    seen.add(s);
    out.push(s);
  }
  return out;
})()"""

# ===========================================================================
# 三、protobuf 最小解码（Python 原生大整数，不存在 JS 的 2^53 精度坑）
# ===========================================================================

SERVICE_NAMES = {
    100: "SEND_MESSAGE", 301: "GET_BY_CONVERSATION", 605: "PARTICIPANTS_LIST",
    610: "GET_CONVERSATION_INFO_LIST", 1001: "GET_STRANGER_LIST",
    2000: "GET_READ_INDEX", 2002: "MARK_READ", 2038: "BATCH_MARK_READ",
    2043: "GET_MESSAGE_BY_INIT", 2048: "GET_USER_MESSAGE",
}


def _read_varint(buf, p):
    """→ (值, 新位置)。字段越界直接抛，调用方按"解到哪算哪"处理。"""
    r = 0
    s = 0
    while True:
        if p >= len(buf):
            raise ValueError("varint overflow")
        b = buf[p]
        p += 1
        r |= (b & 0x7F) << s
        if not (b & 0x80):
            return r, p
        s += 7
        if s > 70:
            raise ValueError("varint too long")


def scan(buf):
    """解一层 protobuf → [(field, wire, value)]；wire=0 时 value 是 int，wire=2 时是 bytes。"""
    out = []
    p = 0
    n = len(buf)
    while p < n:
        try:
            tag, p = _read_varint(buf, p)
        except ValueError:
            break
        field, wire = tag >> 3, tag & 7
        if field == 0:
            break
        try:
            if wire == 0:
                v, p = _read_varint(buf, p)
                out.append((field, wire, v))
            elif wire == 2:
                ln, p = _read_varint(buf, p)
                if p + ln > n:
                    break
                out.append((field, wire, buf[p:p + ln]))
                p += ln
            elif wire == 5:
                if p + 4 > n:
                    break
                p += 4
                out.append((field, wire, None))
            elif wire == 1:
                if p + 8 > n:
                    break
                p += 8
                out.append((field, wire, None))
            else:
                break
        except ValueError:
            break
    return out


def _pick(fs, tag):
    for f in fs:
        if f[0] == tag:
            return f
    return None


def _pick_all(fs, tag):
    return [f for f in fs if f[0] == tag]


def _as_text(sub):
    if not sub:
        return ""
    s = sub.decode("utf-8", errors="replace")
    return "" if "\ufffd" in s else s


def envelope(buf):
    """
    IM 统一回执信封。实测最外层就是信封：1=cmd 2=seq 3=code 4=status 6=业务体 13=自己uid。
    注意：不要先去下钻 f6 再找 code —— 那样读到的 code 是业务体里的。
    """
    r = {"cmd": None, "name": "", "seq": None, "code": None, "status": "", "body": None}
    try:
        fs = scan(buf)
        f = _pick(fs, 1)
        if f and f[1] == 0:
            r["cmd"] = f[2]
            r["name"] = SERVICE_NAMES.get(f[2], f"CMD_{f[2]}")
        f = _pick(fs, 2)
        if f and f[1] == 0:
            r["seq"] = f[2]
        f = _pick(fs, 3)
        if f and f[1] == 0:
            r["code"] = f[2]
        f = _pick(fs, 4)
        if f and f[1] == 2:
            r["status"] = _as_text(f[2])
        f = _pick(fs, 6)
        if f and f[1] == 2:
            r["body"] = f[2]
    except Exception:
        pass
    return r


def _envelope_code(buf):
    """只读返回码（信封字段 3）。8 = 未登录（官方 ERROR_CODE.ERROR_USER_NOT_LOGIN）。"""
    try:
        fs = scan(buf)
    except Exception:
        return None
    f = _pick(fs, 3)
    if f and f[1] == 0:
        return f[2]
    return None


def decode_send_resp(buf):
    """任务三的验收信号。ok = code==0 且 status=='OK'。"""
    env = envelope(buf)
    message_id = None
    server_uuid = None
    self_uid = None
    try:
        if env["body"]:
            inner = _pick(scan(env["body"]), 100)          # f6 → f100
            body = scan(inner[2]) if inner and inner[1] == 2 else []
            f = _pick(body, 1)                             # f100.f1 = message_id(int64)
            if f and f[1] == 0 and f[2] != 0:
                message_id = str(f[2])
            f = _pick(body, 4)                             # f100.f4 = 服务端 UUID
            s = _as_text(f[2]) if f and f[1] == 2 else ""
            if s and re.fullmatch(r"[0-9a-fA-F-]{30,}", s):
                server_uuid = s
        f = _pick(scan(buf), 13)                           # 外层 f13 = 自己 uid
        if f and f[1] == 0 and f[2] and len(str(f[2])) > 4:
            self_uid = str(f[2])
    except Exception:
        pass
    return {
        "ok": env["code"] == 0 and (env["status"] or "").lower() == "ok",
        "code": env["code"], "status": env["status"], "cmd": env["cmd"],
        "message_id": message_id, "server_uuid": server_uuid, "self_uid": self_uid,
    }


def decode_init_resp(buf):
    """任务一的验收信号。complete = has_more is False。"""
    env = envelope(buf)
    has_more = None
    msg_count = 0
    try:
        if env["body"]:
            b = scan(env["body"])
            wrapped = _pick(b, 2043)                       # 兼容被包一层 <2043> 的形态
            if wrapped and wrapped[1] == 2:
                b = scan(wrapped[2])
            f = _pick(b, 2)
            if f and f[1] == 0:
                has_more = (f[2] == 1)
            msg_count = len(_pick_all(b, 1))
    except Exception:
        pass
    return {
        "complete": has_more is False,
        "has_more": has_more, "msg_count": msg_count, "code": env["code"],
    }


def decode_user_info(raw):
    """data[] → 好友资料。remark_name 字段缺失 = 没设备注。"""
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            return []
    if not isinstance(raw, dict):
        return []
    out = []
    for d in (raw.get("data") or []):
        if not isinstance(d, dict):
            continue
        remark = d.get("remark_name") or ""
        out.append({
            "uid": str(d["uid"]) if d.get("uid") is not None else None,
            "sec_uid": d.get("sec_uid") or None,
            "nickname": d.get("nickname") or "",
            "remark": remark,
            "has_remark": bool(remark),
            "unique_id": d.get("unique_id") or "",
        })
    return out


# ===========================================================================
# 四、SSR 登录态（只能正则扫，SSR 里有裸 $undefined，JSON.parse 必崩）
# ===========================================================================

_VIEWS_FIXERS = (
    lambda s: s,
    lambda s: s.replace('\\"', '"'),
    lambda s: unquote(s),
)


def _take(text, pattern, gate=None):
    m = re.search(pattern, text)
    if not m:
        return None
    v = m.group(1)
    if v in ("$undefined", "undefined", "null"):
        return None
    if gate and not gate(v):
        return None
    return v


def scrape_ssr(html):
    """从 chat 页 SSR HTML 抓登录态。返回 {verdict, user_id, nickname, sec_uid, raw}。"""
    out = {"verdict": "unknown", "user_id": None, "nickname": None, "sec_uid": None, "raw": {}}
    if not html:
        return out
    if isinstance(html, (bytes, bytearray)):
        html = html.decode("utf-8", errors="replace")

    raw = out["raw"]
    for view in _VIEWS_FIXERS:
        try:
            v = view(html)
        except Exception:
            continue
        if not isinstance(v, str) or not v:
            continue

        if not raw.get("odin_user_id"):
            raw["odin_user_id"] = _take(
                v, r'"odin"\s*:\s*"\{\\?"user_id\\?"\s*:\s*\\?"([0-9]{5,25})')
            if not raw["odin_user_id"]:
                od = re.search(r'"odin"\s*:\s*"([^"]{0,400})"', v)
                if od:
                    raw["odin_user_id"] = _take(
                        od.group(1).replace('\\"', '"'), r'"user_id"\s*:\s*"?([0-9]{5,25})')
        if raw.get("is_login") is None:
            raw["is_login"] = _take(v, r'"user"\s*:\s*\{\s*"isLogin"\s*:\s*(true|false)')
        if not raw.get("uid"):
            raw["uid"] = _take(
                v,
                r'"user"\s*:\s*\{\s*"isLogin"\s*:\s*(?:true|false)\s*,\s*"info"\s*:\s*\{\s*"uid"\s*:\s*"([^"]*)"',
                gate=lambda x: bool(re.fullmatch(r"[0-9]{5,25}", x)))
        if not raw.get("sec_uid"):
            raw["sec_uid"] = _take(
                v,
                r'"user"\s*:\s*\{\s*"isLogin"\s*:\s*(?:true|false)\s*,\s*"info"\s*:\s*\{[^}]*?"secUid"\s*:\s*"([^"]+)"')
        if not out["nickname"]:
            out["nickname"] = _take(v, r'"nickname"\s*:\s*"([^"$][^"]{0,60})"')
        if raw.get("not_exist_login_cookie") is None:
            raw["not_exist_login_cookie"] = _take(
                v, r'"not_exist_login_cookie"\s*:\s*(true|false)')

    out["user_id"] = raw.get("odin_user_id") or raw.get("uid")
    out["sec_uid"] = raw.get("sec_uid")
    # ⚠️ 顺序要紧：抖音给**未登录访客**也会发 odin.user_id（匿名 id），只凭
    # "抓到一个数字 id" 就判已登录会在扫码页上谎报登录成功（线上实测过：
    # 页面明明是扫码登录框，却打出 [LOGIN] ✅ 已登录 user_id=…）。
    # 所以抖音自己写的 isLogin=false / not_exist_login_cookie=true 具有**否决权**。
    if raw.get("is_login") == "false" or raw.get("not_exist_login_cookie") == "true":
        out["verdict"] = "logged_out"
    elif out["user_id"] and out["user_id"] != "0":
        out["verdict"] = "logged_in"
    elif not out["user_id"] and raw.get("is_login") is None:
        out["verdict"] = "logged_out"
    return out


# ===========================================================================
# 五、身份算法（会话 id ↔ 对方 uid）与归一化
# ===========================================================================

def _is_group(conv_id, type_, participant_count):
    """
    判断是不是群聊。三种依据，优先级从硬到软：
      ① conv_id 里没有 ":"   → 群/特殊会话（实测群聊 id 是 SDK 分配的纯数字，
         单聊恒为 assembleConvId 产出的 `0:1:<小uid>:<大uid>`）
      ② conv_id 前缀 0:1:    → 单聊
      ③ 成员数 > 2
    都对不上返回 None（未知），不要瞎猜。

    ⚠️ `type_` 参数**不参与判断**：实测 `conv.type` 是长整型会话 id
    （如 7209265235419514xxx），不是类型枚举。签名保留是为了向后兼容。
    """
    s = str(conv_id) if conv_id else ""
    if s:
        if ":" not in s:
            return True
        if s.startswith("0:1:"):
            return False
        if s.startswith("0:2:"):
            return True
    if participant_count and participant_count > 2:
        return True
    return None


def peer_uid_of(conv_id, self_uid):
    """单聊对方 uid：conv_id 形如 `0:1:<uidA>:<uidB>`（数值升序），
    跟自己的 uid 比一下就知道哪个是对方。群聊返回 None。"""
    if not conv_id:
        return None
    p = str(conv_id).split(":")
    if len(p) != 4 or p[1] != "1":
        return None
    return p[3] if str(p[2]) == str(self_uid) else p[2]


def _norm(s):
    """归一化：NFKC + 去 NBSP / 零宽 / 空白 + 小写。

    这是**全项目唯一的归一化实现**（旧 `utils.norm` 已删除并入此处）。
    `config.py` 读取配置时不再归一化，只由 `tasks.py` 在匹配前统一归一 ——
    两边都归过才谈得上相等。
    """
    if s is None:
        return ""
    v = unicodedata.normalize("NFKC", str(s))
    v = v.replace("\u3000", " ").replace("\xa0", " ")
    v = v.replace("\u200b", "").replace("\ufeff", "")
    return re.sub(r"\s+", "", v).strip().lower()


# 公开别名：供 core/tasks.py 等外部模块导入（_norm 是内部名，留着的调用点太多）
norm = _norm

# 子串兜底的**分段**切法：空白与常见中英标点都算边界。
# 只做「整段相等」，不做任意子串 —— 理由见 `_match` 里子串兜底那段注释。
_FUZZY_SPLIT = re.compile(
    r"[\s()（）\[\]【】{}<>《》「」『』,，.。、;；:：!！?？/\\|+*~=_—－–-]+")


def target_ref(raw):
    """配置里的一项发送目标 → 统一结构（**带身份的目标只按身份匹配**）。

    两种写法都要能用：

    | `.env` 里的形态 | 来源 | 匹配方式 |
    |---|---|---|
    | `"张三"` | 手写、旧配置、A 的方案 | 名字（备注/昵称/抖音号/标题，最后有条子串兜底）|
    | `{"name": "张三", "uid": "…", "conv_id": "…"}` | 面板勾选好友列表写入 | **只认 `conv_id` / `uid`**，名字只用来显示 |

    为什么要有第二种：名字会改、会重名、还会被子串兜底误伤（目标「小明」
    命中「小明明」），而 `conv_id` 与 `uid` 是抖音自己算出来的，选中之后
    `strict_verify` 校验的还是同一个 id —— 只有 id 对得上，「我要的人」
    和「选中的人」才保证是同一个。
    """
    if isinstance(raw, dict):
        name = str(raw.get("name") or raw.get("display") or "").strip()
        uid = str(raw.get("uid") or "").strip()
        conv_id = str(raw.get("conv_id") or "").strip()
        if not (name or uid or conv_id):
            return None
        if conv_id:
            key = "conv:" + conv_id
        elif uid:
            key = "uid:" + uid
        else:
            key = _norm(name)
        return {"name": name or conv_id or uid, "uid": uid,
                "conv_id": conv_id, "key": key}
    text = str(raw if raw is not None else "").strip()
    if not text:
        return None
    return {"name": text, "uid": "", "conv_id": "", "key": _norm(text)}


def target_refs(items):
    """把配置里的目标列表整体归一，丢掉空项。"""
    out = []
    for raw in items or []:
        ref = target_ref(raw)
        if ref:
            out.append(ref)
    return out


def target_name(item):
    """目标的人话名字（账本、日志、面板都显示它）。"""
    if isinstance(item, dict):
        return str(item.get("name") or item.get("conv_id") or item.get("uid") or "")
    return str(item or "")


def split_message_lines(text):
    """把消息正文断成「要逐行键入」的段列表。两种换行都认。

    链路上有两种 `\\n`，来自不同源头，必须都处理：

    | 形态 | 例子 | 来源 |
    |---|---|---|
    | **字面** `\\` + `n` | `"甲\\n乙"` | `.env` 的 `MESSAGE_TEMPLATE` —— `dotenv` 读出来**不做 unescape** |
    | **真**换行 `U+000A` | `"甲\\n乙"` | 第三方内容（一言 API 正文）、手写 `.env` 的引号多行值 |

    只认字面 `\\n` 会把真换行**静默吞掉**（三行挤成一行）；只认真换行会让
    `.env` 的默认模板整段变成一行。所以先统一、再断。

    ★ 三条 replace 的顺序**无所谓**（实测可交换）：字面 `\\r\\n` 是 `\\`+`r`+`\\`+`n`
    四个字符，里面没有真的 `U+000D`，所以「真 CRLF → 真 LF」那条规则对字面串
    无从下手；反过来，「字面 `\\r\\n` → 字面 `\\n`」对真 CRLF 也匹配不到。
    **两条规则作用在互不相交的字符集上。** 这里按「先字面、后真换行」写只是为了读着顺。

    空段由调用方的 `if line:` 挡掉（不键入），但**段间仍会按一次 `Shift+Enter`** ——
    因为那确实是两个换行符，视觉上就该空一行。
    """
    return (
        str(text)
        .replace("\\r\\n", "\\n")
        .replace("\r\n", "\n")
        .replace("\\n", "\n")
        .split("\n")
    )


# ---------------------------------------------------------------------------
# 脱敏工具：给自测 / 单测生成「格式合法但非真实」的假身份，
# 避免把真实 uid / 昵称 / 会话 id 写进代码或测试。
# ---------------------------------------------------------------------------

# 固定种子 → 每次运行结果一致，断言可以写死；同时又不是任何真值。
_FAKE_RNG_SEED = 20260919


def fake_uid(rng=None, digits=17):
    """生成形如抖音 uid 的十进制串（真实 uid 为 17 位左右的 int64）。"""
    import random
    r = rng or random.Random(_FAKE_RNG_SEED)
    return str(r.randrange(10 ** (digits - 1), 10 ** digits))


def fake_sec_uid(rng=None, body_len=48):
    """生成形如 `MS4wLjABAAAA…` 的 sec_uid（固定前缀 + 随机 URL-safe 体）。"""
    import random
    import string
    r = rng or random.Random(_FAKE_RNG_SEED)
    alphabet = string.ascii_letters + string.digits + "-_"
    return "MS4wLjABAAAA" + "".join(r.choice(alphabet) for _ in range(body_len))


def fake_conv_id(rng=None, uid_a=None, uid_b=None):
    """生成单聊 conv_id：`0:1:<小uid>:<大uid>`（数值升序，同官方 assembleConvId）。"""
    import random
    r = rng or random.Random(_FAKE_RNG_SEED)
    a = int(uid_a) if uid_a else int(fake_uid(r))
    b = int(uid_b) if uid_b else int(fake_uid(r))
    if b < a:
        a, b = b, a
    return f"0:1:{a}:{b}"


# 自测里用的固定假身份（固定种子派生，写死断言可读）
_FAKE_ME = "10000000000000001"
_FAKE_PEER_A = "10000000000000002"
_FAKE_PEER_B = "10000000000000003"


# ===========================================================================
# 六、监听器：只读旁路，page.on("response") 抓 5 类响应
# ===========================================================================

class ImMonitor:
    """把"网页自己发出去的请求"翻译成三个任务需要的结论。全程只读，不改任何请求。"""

    # 只留真正要用的：登录态、列表完整、好友资料、发送回执、会话基本信息
    ROUTES = (
        ("login", re.compile(r"^https://www\.douyin\.com/(chat|$|\?)")),
        ("send", re.compile(r"imapi\.douyin\.com/v1/message/send")),
        ("init", re.compile(r"/v1/message/get_message_by_init")),
        ("userInfo", re.compile(r"/aweme/v1/web/im/user/info/")),
        ("convInfo", re.compile(r"/v2/conversation/get_info_list")),
    )

    def __init__(self, page):
        self.page = page
        self.login = {"verdict": "unknown", "user_id": None, "nickname": None, "sec_uid": None}
        self.list_rounds = []
        self.list_complete = False
        self.list_has_more = None
        self.peers = {}          # sec_uid|uid → 资料
        self.peer_order = []
        self.sends = []
        self.hits = {}
        self.errors = []
        page.on("response", self._on_response)

    # ---- 内部 ----
    def _on_response(self, resp):
        try:
            url = resp.url
        except Exception:
            return
        route = None
        for rid, rx in self.ROUTES:
            if rx.search(url):
                route = rid
                break
        if route is None:
            return

        try:
            body = resp.body()
        except Exception as e:
            self.errors.append((url, str(e)))
            return
        if not body:
            # 空响应体以前是**静默丢弃**的：2026-10-09 wheat 掉登录那一轮，4 条发送
            # 请求里有 3 条的响应体是空的，日志上一行痕迹都没有，事后只能靠"没有回执"
            # 反推。发送路由的空响应是**失败信号本身**，按 warning 记；其它路由 debug。
            (logger.warning if route == "send" else logger.debug)(
                f"[NET] {route} 响应体为空  url={url}")
            return

        self.hits[route] = self.hits.get(route, 0) + 1
        try:
            if route == "login":
                self._handle_login(body)
            elif route == "send":
                self._handle_send(body)
            elif route == "init":
                self._handle_init(body)
            elif route == "userInfo":
                self._handle_user_info(body)
            else:
                env = envelope(body)
                logger.debug(f"[CONV] cmd={env['name']} code={env['code']}")
        except Exception as e:
            self.errors.append((url, str(e)))

    def _handle_login(self, body):
        r = scrape_ssr(body)
        if r["verdict"] == "logged_in" or self.login["verdict"] == "unknown":
            self.login = r
        label = {"logged_in": "✅ 已登录", "logged_out": "⛔ 未登录", "unknown": "❓ 未知"}
        logger.info(f"[LOGIN] {label.get(self.login['verdict'])}  user_id={self.login['user_id'] or '-'}"
                    + (f"  nickname={self.login['nickname']}" if self.login["nickname"] else ""))

    def _handle_send(self, body):
        r = decode_send_resp(body)
        r["at"] = time.time()
        self.sends.append(r)
        _msg = (f"[SEND] {'✅ 成功' if r['ok'] else '❌ 失败'}  code={r['code']}  "
                f'status="{r["status"]}"'
                + (f"  message_id={r['message_id']}" if r["message_id"] else ""))
        if not r["ok"]:
            # 解不出来的时候把**原始响应体**（截断）打出来：只剩 `code=None status=""`
            # 一行，事后完全无从判断对面到底说了什么、还是压根没说话
            # （2026-10-09 wheat / 2026-10-10 🌈LBX🌈 的发送失败就卡在这里）。
            try:
                raw = bytes(body or b"").decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                raw = repr(body)
            _msg += f"  raw={raw[:240]!r}"
        (logger.info if r["ok"] else logger.warning)(_msg)

    def _handle_init(self, body):
        r = decode_init_resp(body)
        self.list_rounds.append(r)
        self.list_has_more = r["has_more"]
        if r["complete"]:
            self.list_complete = True
        logger.debug(f"[LIST] 第 {len(self.list_rounds)} 轮  messages={r['msg_count']}  "
                     f"has_more={r['has_more']}  → "
                     + ("✅ 列表已加载完整" if r["complete"] else "⏳ 还有下一页"))

    def _handle_user_info(self, body):
        peers = decode_user_info(body)
        added = 0
        for p in peers:
            key = p["sec_uid"] or p["uid"]
            if not key:
                continue
            if key not in self.peers:
                self.peer_order.append(key)
                added += 1
            self.peers[key] = p
        if added:
            logger.debug(f"[PEER] 新增 {added} 人（累计 {len(self.peer_order)}）")
            for k in self.peer_order[-added:]:
                p = self.peers[k]
                logger.debug(
                    f"        {p['nickname'] or '(无昵称)'}"
                    + (f"  备注={p['remark']}" if p["has_remark"] else "  (无备注)")
                    + f"  uid={p['uid']}  抖音号={p['unique_id'] or '-'}")

    # ---- 对外 ----
    def by_sec_uid(self):
        return {p["sec_uid"]: p for p in self.peers.values() if p["sec_uid"]}

    def by_uid(self):
        return {p["uid"]: p for p in self.peers.values() if p["uid"]}

    def snapshot(self):
        peers = [self.peers[k] for k in self.peer_order]
        return {
            "login": dict(self.login),
            "list": {"complete": self.list_complete, "has_more": self.list_has_more,
                     "rounds": len(self.list_rounds)},
            "peers": peers,
            "peer_count": len(peers),
            "with_remark": [p for p in peers if p["has_remark"]],
            "sends": self.sends,
            "hits": dict(self.hits),
        }

    def detach(self):
        try:
            self.page.remove_listener("response", self._on_response)
        except Exception:
            pass


def attach(page):
    """挂监听。必须在 page.goto 之前调用，否则首屏的响应会漏掉。"""
    return ImMonitor(page)


# ===========================================================================
# 七、登录态判定（任务一的输入）
# ===========================================================================

LOGIN_LOGS = {
    "LOGGED_IN": "[LOGIN] ✅ 已登录",
    "NOT_LOGGED_IN": "[LOGIN] ❌ 未登录（没有 sessionid）→ 需要扫码登录",
    "EXPIRED": "[LOGIN] ⚠️ 登录已失效（有 sessionid 但服务端不认）→ 需要重新登录",
    "UNKNOWN": "[LOGIN] ❓ 登录态未知（SSR 与 DOM 都没给出结论）",
}


def check_login(page, mon):
    """
    登录态判定：SSR/监听 → DOM（既兜底也**反证**） → cookie 佐证。
    返回 {state, user_id, nickname, sec_uid, login_box, log}；结论行由调用方按级别记日志。

    反证不可省：抖音会给**未登录访客**也发 `odin.user_id`（匿名 id），只看"有没有
    数字 id"就会在扫码页上谎报"已登录"，然后白等两分钟列表就绪再超时跳过。
    """
    state, user_id, nickname, sec_uid = "UNKNOWN", None, None, None

    # ① 监听拿到的 SSR（最准）
    li = mon.login if mon else {}
    if li.get("verdict") == "logged_in" and li.get("user_id"):
        state, user_id, nickname, sec_uid = ("LOGGED_IN", li["user_id"],
                                             li.get("nickname"), li.get("sec_uid"))

    # ② 自己再扫一遍页面 HTML（SSR 脚本节点还在 DOM 里）
    if state == "UNKNOWN":
        try:
            html = page.content()
        except Exception:
            html = ""
        s = scrape_ssr(html)
        if s["verdict"] == "logged_in" and s["user_id"]:
            state, user_id, nickname, sec_uid = ("LOGGED_IN", s["user_id"],
                                                 s["nickname"], s["sec_uid"])
        elif s["verdict"] == "logged_out":
            state = "NOT_LOGGED_IN"

    # ③ DOM：既做兜底，也做**反证**
    #    页面上明摆着画着扫码登录框 → 不管 SSR/监听里那个数字 id 是谁的，都不算已登录。
    #    （曾出现"扫码页上谎报 [LOGIN] ✅ 已登录"，随后白等两分钟列表就绪。）
    try:
        dom = page.evaluate(JS_LOGIN_DOM)
    except Exception:
        dom = {}
    login_box = bool(dom.get("loginVisible") or dom.get("loginNew"))
    if state == "LOGGED_IN" and login_box and not dom.get("avatarCard"):
        state, user_id, nickname, sec_uid = "NOT_LOGGED_IN", None, None, None

    if state == "UNKNOWN":
        if login_box:
            state = "NOT_LOGGED_IN"
        elif dom.get("avatarCard") or dom.get("hasChatRoot"):
            state = "LOGGED_IN"      # 有聊天根节点，但没拿到 uid
            nickname = nickname or None

    # ④ cookie 佐证：有 sessionid 但判未登录 → 判为"已失效"而不是"未登录"
    if state == "NOT_LOGGED_IN":
        try:
            names = [c.get("name", "") for c in page.context.cookies()]
        except Exception:
            names = []
        if any(n.startswith("sessionid") for n in names):
            state = "EXPIRED"

    line = LOGIN_LOGS.get(state, LOGIN_LOGS["UNKNOWN"])
    if state in ("LOGGED_IN", "EXPIRED") and user_id:
        line += f"  user_id={user_id}"
    if nickname:
        line += f"  nickname={nickname}"

    return {"state": state, "user_id": user_id, "nickname": nickname,
            "sec_uid": sec_uid, "login_box": login_box, "log": line}


# ---------------------------------------------------------------------------
# 就绪状态与结果
# ---------------------------------------------------------------------------

STATUS_READY = "READY"
STATUS_LOGGED_OUT = "LOGGED_OUT"
STATUS_EXPIRED = "EXPIRED"
STATUS_LOGIN_LOST = "LOGIN_LOST"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_ERROR = "ERROR"

READY_STATUSES = (STATUS_READY, STATUS_LOGGED_OUT, STATUS_EXPIRED,
                  STATUS_LOGIN_LOST, STATUS_TIMEOUT, STATUS_ERROR)


class DouyinIM:
    """会话列表扫描器。

    用法::

        im = DouyinIM(page)                       # 挂钩子 + goto + 跑门禁
        res = im.wait_ready()                     # 阻塞等门禁结论
        if res.status != "READY":
            return
        for hit in im.iter_find_and_select(["甲同学", "乙同学"]):
            im.type_and_send(hit, "在吗")          # 此刻会话已选中
        print(im.last_scan)
    """

    def __init__(self, page, url=CHAT_URL, timeout=90.0, max_steps=200,
                 settle_ms=800, ready_timeout=30.0, wait_until="domcontentloaded",
                 auto_goto=True):
        self.page = page
        self.url = url
        self.timeout = timeout              # 扫描总预算（秒）
        self.max_steps = max_steps          # 滚动步数硬上限
        self.settle_ms = settle_ms          # 资料静默窗（毫秒）
        self.ready_timeout = ready_timeout  # 门禁等待上限（秒）

        self._state = None                  # 就绪结果缓存
        self._callbacks = []
        self._login_lost = []
        self._select_failed = []
        self._detached = False
        self.last_scan = None
        self._scan_cache = {}               # conv_id -> hit（跨调用复用）
        self._walk_stat = {"stopped": "not-run", "steps": 0, "seen": {}}
        # 最近一次「选中校验」与「发送回执」的裁决（strict_verify.Verdict）。
        # 只作诊断/仪表盘用，不参与分支；调用方要用的是返回值本身。
        self.last_select_verdict = None
        self.last_receipt_verdict = None
        # 输入框里"可能有没清干净的自己写的字" —— 只由本进程记账，因为 Draft.js
        # 的内容真相在它自己的 EditorState 里、DOM 判空会被骗（见 _clear_editor）。
        self._editor_dirty = False
        # 发送全都没回执时**复检**出来的"登录已失效"（见 recheck_login）。
        # 一旦置位，本轮剩下的目标不再白等 20 秒超时 —— 已经没登录了，
        # 继续点发送只会让风控更难看（2026-10-09 wheat 掉登录那次就是这么烧的）。
        self._login_dead = False
        self._login_dead_reason = ""

        # ① 先挂钩子，再导航。顺序反了会漏首屏响应。
        self.mon = ImMonitor(page)
        self._watch_login_lost()

        if auto_goto:
            self._goto(wait_until)

        # ② 门禁在构造里就跑起来，on_ready 注册晚了也能立刻拿到结论
        self._state = self._run_preflight()

    # ---------------------------------------------------------------- 内部

    def _goto(self, wait_until):
        try:
            self.page.goto(self.url, wait_until=wait_until)
        except Exception as e:
            logger.warning(f"[IM] ⚠️ 导航异常（继续尝试判定）：{e}")
        # 留一点时间给首屏脚本与弹窗
        try:
            self.page.wait_for_timeout(1200)
        except Exception:
            pass

    def _watch_login_lost(self):
        """运行期掉登录监控：IM 响应 status_code=8 是官方「掉登录」码。

        挂在 response 上而不是轮询，是为了「任意时刻」都能捕获。
        """
        def on_resp(resp):
            try:
                url = resp.url
            except Exception:
                return
            if "imapi.douyin.com" not in url and "/im/" not in url:
                return
            try:
                body = resp.body()
            except Exception:
                return
            if not body or len(body) > 4096:
                return
            code = _envelope_code(body)
            if code == 8:
                self._emit_login_lost(code)
        try:
            self.page.on("response", on_resp)
        except Exception:
            pass
        self._on_resp_login_lost = on_resp

    def _emit_login_lost(self, code, reason=""):
        head = (f"[LOGIN] ⛔ 运行期检测到掉登录（status_code={code}）" if code is not None
                else "[LOGIN] ⛔ 运行期检测到掉登录（发送失败后复检）")
        logger.error(head + (f"  {reason}" if reason else ""))
        for cb in list(self._login_lost):
            try:
                cb({"code": code, "at": time.time(), "reason": reason})
            except Exception:
                traceback.print_exc()

    def recheck_login(self):
        """发送拿不到回执时**主动复检**登录态，返回 `check_login()` 的结论。

        为什么必须在发送失败时再查一次：2026-10-09 wheat 的事故里，网页登录态失效
        之后页面**仍然长着 IM 的样子**（会话列表、输入框、发送按钮一应俱全），于是
        点击照样"成功"、回执永远不来，4 个目标被记成「发送失败」而不是「登录失效」，
        整轮还按 `status=ok` 写进时间表 —— 面板上一切正常，人却在那边干等消息，
        而且当天再也不会重试。掉登录和发送失败必须分开报。
        """
        lg = check_login(self.page, self.mon)
        state = str((lg or {}).get("state") or "UNKNOWN")
        if state != "LOGGED_IN":
            self._login_dead = True
            self._login_dead_reason = state
            self._emit_login_lost(None, reason=f"发送失败后复检登录态 = {state}")
        return lg

    def _run_preflight(self):
        """两道门禁：登录态 → 列表就绪。"""
        logger.info("─" * 64)
        logger.info("抖音 IM · 操作前检查")
        logger.info("─" * 64)

        lg = check_login(self.page, self.mon)
        state = lg["state"]
        (logger.info if state in ("LOGGED_IN",) else logger.warning)(lg["log"])
        if state in ("NOT_LOGGED_IN",):
            return self._finish(STATUS_LOGGED_OUT, lg)
        if state in ("EXPIRED",):
            return self._finish(STATUS_EXPIRED, lg)

        ls = self._wait_list_ready(lg)
        if not ls["ready"]:
            # 列表等不到**不等于**超时：登录失效时页面既没有会话列表，也可能
            # 没有我们认得出的登录框（2026-10 新版弹窗）。再判一次登录态，
            # 把"登录已失效"从"等待超时"里分出来 —— 面板上"超时"没法告诉用户
            # 该去重新扫码，而 wheat 2026-10-09 那天正是被报成超时的。
            again = ls.get("login_recheck") or {}
            again_state = str(again.get("state") or "")
            if again_state in ("NOT_LOGGED_IN", "EXPIRED"):
                return self._finish(
                    STATUS_LOGGED_OUT if again_state == "NOT_LOGGED_IN" else STATUS_EXPIRED,
                    again, ls,
                )
            return self._finish(STATUS_TIMEOUT, lg, ls)

        return self._finish(STATUS_READY, lg, ls)

    def _wait_list_ready(self, lg):
        """列表就绪：DOM 连续两次采样稳定即可。

        注意与「列表全量」的区别 —— 首屏只渲染一页（约 20-25 条），
        后续靠滚动补。这里只回答「能不能开始滚」。
        """
        t0 = time.time()
        last = None
        result = {"ready": False, "via": None, "count": 0, "net_complete": False}
        while time.time() - t0 < self.ready_timeout:
            try:
                dom = self.page.evaluate(JS_LIST_READY) or {}
            except Exception:
                dom = {}
            if dom.get("ready"):
                now = time.time()
                if last and last["count"] == dom["count"] and now - last["at"] >= 1.2:
                    result = {"ready": True, "via": "dom-stable",
                              "count": dom["count"],
                              "net_complete": bool(self.mon.list_complete)}
                    break
                if not last or last["count"] != dom["count"]:
                    last = {"count": dom["count"], "at": now}
            try:
                self._snooze(300, "等列表就绪")
            except Exception:
                time.sleep(0.3)

        if result["ready"]:
            logger.info(f"[LIST] ✅ 会话列表就绪（{result['via']}）"
                        f"  当前渲染 {result['count']} 个会话"
                        f"  has_more={self.mon.list_has_more}")
        else:
            logger.warning(f"[LIST] ⚠️ {self.ready_timeout:.0f}s 内未判定列表就绪")
            # 超时前复检登录态：报"超时"对用户没有指导意义，报"登录已失效"
            # 才指向唯一的出路（重新扫码）。结论交给 `_run_preflight` 决定终态。
            try:
                again = check_login(self.page, self.mon)
            except Exception:
                again = {}
            result["login_recheck"] = again
            if str(again.get("state") or "") in ("NOT_LOGGED_IN", "EXPIRED"):
                logger.error(f"[LOGIN] ⛔ 列表等不到，复检登录态="
                             f"{again.get('state')} —— 需要重新扫码登录")
        return result

    def _finish(self, status, lg, ls=None):
        self._state = {
            "status": status,
            "user_id": lg.get("user_id"),
            "nickname": lg.get("nickname"),
            "sec_uid": lg.get("sec_uid"),
            "login_state": lg.get("state"),
            "list": ls or {"ready": False, "count": 0},
            "log": lg.get("log"),
        }
        tag = {"READY": "✅ 就绪", "LOGGED_OUT": "⛔ 未登录",
               "EXPIRED": "⚠️ 登录已失效", "LOGIN_LOST": "⛔ 掉登录",
               "TIMEOUT": "⚠️ 超时", "ERROR": "❌ 出错"}.get(status, status)
        (logger.info if status == STATUS_READY else logger.error)(
            f"[IM] {tag}  user_id={self._state['user_id'] or '-'}"
            + (f"  nickname={self._state['nickname']}" if self._state["nickname"] else ""))
        for cb in list(self._callbacks):
            try:
                cb(dict(self._state))
            except Exception:
                traceback.print_exc()
        return self._state

    # ---------------------------------------------------------- 对外：就绪

    def on_ready(self, cb):
        """注册就绪回调。若门禁已经出结果，立刻回调一次（不重复投递）。"""
        self._callbacks.append(cb)
        if self._state is not None:
            try:
                cb(dict(self._state))
            except Exception:
                traceback.print_exc()
        return self

    def on_login_lost(self, cb):
        """注册运行期掉登录回调。"""
        self._login_lost.append(cb)
        return self

    def wait_ready(self):
        """返回就绪结果（构造里已算完，这里只是取）。"""
        return dict(self._state or {})

    @property
    def ready(self):
        return bool(self._state and self._state.get("status") == STATUS_READY)

    @property
    def self_uid(self):
        return (self._state or {}).get("user_id")

    # ---------------------------------------------------- 对外：滚动找人

    def iter_find_and_select(self, targets):
        """从头滚到尾，逐个找出目标并选中。

        yield 出来的那一刻，该会话**已被选中且 conv_id 已校验**。
        调用方拿到就能直接输入发送，不需要再次定位。

        targets：原始关键词列表（备注 / 昵称 / 抖音号 / uid 皆可，内部会归一化）。
        也可以给 `{"name", "uid", "conv_id"}` 对象（面板勾选好友列表写入的形态）——
        这种目标**只按 id 匹配**，名字不再参与判定，见 `target_ref()`。
        """
        pending = {}
        for ref in target_refs(targets):
            pending.setdefault(ref["key"], ref)
        found = {}

        by_conv = {r["conv_id"]: r["key"] for r in pending.values() if r["conv_id"]}
        by_uid = {}
        for r in pending.values():
            if r["uid"]:
                by_uid.setdefault(r["uid"], r["key"])

        logger.info("─" * 64)
        logger.info(f"抖音 IM · 滚动查找 {len(pending)} 个目标"
                    f"（其中 {len(by_conv)} 个按 conv_id 精确匹配）")
        logger.info("─" * 64)

        self._select_failed = []

        for new_items in self._walk():
            # 对「已见过但还没归并资料」的项补齐，再统一匹配
            for item in new_items:
                self._join(item)

            # 匹配：id（conv_id/uid）> 备注 > 昵称 > 抖音号 > 标题 > 子串
            for item in new_items:
                key, how = self._match(item, pending, by_conv, by_uid)
                if not key:
                    continue
                item["matched_by"] = how
                pending.pop(key, None)
                found[key] = item
                logger.info(f"[SCAN] ✅ 命中 {item['display']}"
                            f"（依据={how}）conv_id={item['conv_id']}")

                ok = self._select_and_verify(item)
                if not ok:
                    logger.warning(f"[SCAN] ⚠️ {item['display']} 选中失败，继续扫描")
                    self._select_failed.append(item)
                    continue

                item["reselect"] = self._make_reselect(item)
                yield item

                if not pending:
                    self._walk_stat["stopped"] = "all-found"
                    logger.info(f"[SCAN] 🎯 {len(found)} 个目标全部找到，提前停止")
                    break
            if self._walk_stat["stopped"] == "all-found":
                break

        self._finish_scan(found, [r["name"] for r in pending.values()])

    # ------------------------------------------------------------ 滚动骨架

    def _walk(self):
        """滚动会话列表，逐轮产出「本轮新出现」的会话项（已按 conv_id 去重）。

        滚动的全部复杂度只在这一处：回到顶部、超时、读窗失败重试、到底判定、
        步长守卫、虚拟窗口换页等待、到底后等防抖再比高度。
        虚拟列表下同一条会被重叠窗口反复读到，这里只在**首次出现**时产出一次。

        调用方想提前停就 break；结束统计一律写进 self._walk_stat。
        """
        probe = self._scroll_probe()
        if not probe.get("found"):
            logger.error("[SCAN] ❌ 找不到会话列表容器")
            self._walk_stat = {"stopped": "no-container", "steps": 0,
                               "seen": dict(self._scan_cache)}
            return

        self._scroll_to(0)                              # 回到顶部，保证从头扫
        self._snooze(400, "回到顶部后停顿")

        seen = dict(self._scan_cache)
        window = []
        steps = 0
        empty_rounds = 0
        failed_windows = 0
        stopped = "exhausted"
        finished = False        # while 自然跑完才算扫全；被外部 break 不算
        t0 = time.time()
        try:
            while steps < self.max_steps:
                if time.time() - t0 > self.timeout:
                    stopped = "timeout"
                    logger.warning(f"[SCAN] ⏱ 扫描超时（{self.timeout:.0f}s）")
                    break

                window = self._read_window()
                if window is None:
                    failed_windows += 1
                    logger.warning(f"[SCAN] ⚠️ 读取窗口失败（{failed_windows}/5）")
                    if failed_windows >= 5:
                        stopped = "read-error"
                        break
                    self._snooze(500, "读窗口失败退避")
                    continue
                failed_windows = 0

                new_items = []
                for item in window:
                    cid = item.get("conv_id")
                    if not cid or cid in seen:
                        continue
                    seen[cid] = item                    # 按 conv_id 去重
                    new_items.append(item)
                if new_items:
                    self._settle()                      # 等 /im/user/info 落定

                yield new_items

                # ---- 推进 ----
                probe = self._scroll_probe()
                if not probe.get("found"):
                    stopped = "container-lost"
                    break

                if probe.get("atBottom"):
                    self._snooze(1000, "到底防抖")    # 跨过前端 1000ms 防抖
                    after = self._scroll_probe()
                    if after.get("scrollHeight", 0) <= probe.get("scrollHeight", 0):
                        logger.info("[SCAN] 🏁 已到底且高度不再增长，扫描结束")
                        stopped = "reach-bottom"
                        break
                    logger.debug("[SCAN] ⬇ 到底后高度增长，继续加载")
                    empty_rounds = 0
                    continue

                step = max(120, int(probe["clientHeight"] * 0.4))
                nxt = min(probe["scrollTop"] + step,
                          max(0, probe["scrollHeight"] - probe["clientHeight"]))
                self._scroll_to(nxt)
                steps += 1
                self._snooze(400, "等虚拟窗口换页")

                moved = self._scroll_probe().get("scrollTop")
                if moved == probe.get("scrollTop"):
                    empty_rounds += 1
                    logger.debug(
                        f"[SCAN] scrollTop 未变化（{moved}），已到底计数 {empty_rounds}/3")
                    if empty_rounds >= 3:
                        stopped = "no-move"
                        break
                else:
                    empty_rounds = 0
                    logger.debug(f"[SCAN] 步 {steps}：scrollTop {probe['scrollTop']} → {moved}"
                                 f"（窗口 {len(window)} 项，累计 {len(seen)}）")
            finished = True     # 只有 while 条件跑干才会走到这，break 不会
        finally:
            # 「stopped 还是初始值」+「while 没跑完」= 调用方提前 break，
            # 不能报 scanned_all —— 那会让 missing 被误读成"确实不存在"。
            if not finished and stopped == "exhausted":
                stopped = "caller-break"
            self._scan_cache = seen
            self._walk_stat = {"stopped": stopped, "steps": steps, "seen": seen}

    def _finish_scan(self, found, missing):
        """统一组装 last_scan（两种扫描共用）。"""
        st = self._walk_stat
        seen = st["seen"]
        stopped = st["stopped"]
        # all-found 是调用方提前 break 时写进去的，同样算「扫全了」
        scanned_all = stopped in ("reach-bottom", "all-found", "no-move", "exhausted")
        # note 要数据驱动：主列表扫完并不代表"漏了折叠组"，
        # 这个账号可能压根没有折叠组/陌生人组（实测就是 0），
        # 无条件写一句"未覆盖折叠组/陌生人组"会让人以为读漏了。
        note = "扫描未完成，missing 不代表不存在"
        if scanned_all:
            groups = self.fold_groups()
            counts = [v for v in groups.values() if isinstance(v, int)]
            if not counts:
                note = "未覆盖折叠组/陌生人组"          # 探不到就保守提示
            elif sum(counts):
                note = (f"主列表已读完；另有折叠组/陌生人组未包含"
                        f"（{sum(counts)} 个分组）")
            else:
                note = ""                              # 确实没有，不用吓人
        self.last_scan = {
            "found": found,
            "missing": missing,
            "select_failed": [c.get("display") for c in self._select_failed],
            "scanned_all": scanned_all,
            "steps": st["steps"],
            "stopped": stopped,
            "visited": len(seen),
            "note": note,
        }
        logger.info(f"[SCAN] 结束：stopped={stopped} 步数={st['steps']} 访问={len(seen)} "
                    f"找到={len(found)} 未找到={len(missing)} scanned_all={scanned_all}")

        # 监听层的响应读取失败此前从不打印，卡顿排查没有线索
        if self.mon.errors:
            logger.warning(
                f"[SCAN] ⚠️ 监听层有 {len(self.mon.errors)} 条响应读取失败（卡顿多半来自这里）："
            )
            for url, err in self.mon.errors[:5]:
                logger.warning(f"        {url} → {err}")
            if len(self.mon.errors) > 5:
                logger.warning(f"        …另有 {len(self.mon.errors) - 5} 条")

    def iter_conversations(self):
        """从头滚到尾，逐个产出**全部**会话（任务二：采集会话）。

        yield 的项已 `_join` 过监听资料，字段与 `iter_find_and_select` 的命中项一致：
        display / remark / nickname / douyin_id / uid / sec_uid / conv_id /
        is_group / unread / is_muted / data_index。

        按 conv_id 去重，虚拟列表的重叠窗口不会重复产出。
        结束后查 `last_scan`：`scanned_all=False` 表示没扫完，结果只是「已扫到的」。
        """
        self._select_failed = []
        logger.info("─" * 64)
        logger.info("抖音 IM · 枚举全部会话")
        logger.info("─" * 64)

        for new_items in self._walk():
            for item in new_items:
                self._join(item)
                yield item

        self._finish_scan({}, [])

    def _read_window(self):
        t0 = time.monotonic()
        try:
            rows = self.page.evaluate(JS_COLLECT) or []
        except Exception as e:
            # 补上「等了多久」和「为什么失败」—— 原本调用方只说「读取窗口失败（n/5）」
            logger.warning(
                f"[SCAN] ⚠️ 读窗口失败（耗时 {time.monotonic() - t0:.1f}s）：{_brief(e)}"
            )
            return None
        _slow_warn("读窗口(JS_COLLECT)", time.monotonic() - t0)
        out = []
        for r in rows:
            out.append({
                "conv_id": r.get("convId"),
                "title": r.get("title"),
                "nickname": None,
                "remark": None,
                "douyin_id": "",
                "uid": r.get("uid"),
                "sec_uid": r.get("secUid"),
                "short_id": r.get("shortId"),
                "is_group": _is_group(r.get("convId"), r.get("type"),
                                     r.get("participantCount")),
                "unread": r.get("unread"),
                "is_muted": r.get("isMuted"),
                "is_current": r.get("isCurrent"),
                "data_index": r.get("dataIndex"),
                "dom_index": r.get("index"),
                "from_fiber": r.get("fromFiber"),
                "display": r.get("title"),
                "matched_by": None,
            })
            # 单聊缺 uid 时从 conv_id 拆
            if out[-1]["uid"] is None and out[-1]["conv_id"]:
                out[-1]["uid"] = self._peer_uid_of(out[-1]["conv_id"])
        return out

    def _peer_uid_of(self, conv_id):
        """单聊 conv_id = 0:1:<uidA>:<uidB>，取不是自己的那个。"""
        me = self.self_uid
        if not conv_id or not me:
            return None
        return peer_uid_of(conv_id, me)

    def _join(self, item):
        """把监听拿到的 /im/user/info 资料合并进会话项（按 sec_uid → uid）。"""
        peer = None
        if item.get("sec_uid"):
            peer = self.mon.by_sec_uid().get(item["sec_uid"])
        if peer is None and item.get("uid"):
            peer = self.mon.by_uid().get(item["uid"])
        if peer:
            item["nickname"] = peer.get("nickname") or None
            item["remark"] = peer.get("remark") or None
            item["douyin_id"] = peer.get("unique_id") or ""
            item["sec_uid"] = item.get("sec_uid") or peer.get("sec_uid")
            item["uid"] = item.get("uid") or peer.get("uid")
        item["display"] = item.get("remark") or item.get("nickname") or item.get("title")
        return item

    def _settle(self):
        """等资料静默窗：最后一次 /im/user/info 之后 settle_ms 内无新增，视为落定。"""
        if not self.settle_ms:
            return
        deadline = time.time() + self.settle_ms / 1000.0
        last_count = len(self.mon.peer_order)
        quiet_since = time.time()
        while time.time() < deadline:
            self._snooze(120, "资料静默窗")
            now_count = len(self.mon.peer_order)
            if now_count != last_count:
                last_count = now_count
                quiet_since = time.time()
            if (time.time() - quiet_since) * 1000 >= self.settle_ms:
                return

    def _match(self, item, pending, by_conv=None, by_uid=None):
        """匹配目标。顺序：**conv_id > uid > 备注 > 昵称 > 抖音号 > 标题 > 子串**。

        前三档里，前两档是「按 id 精确匹配」—— 只有面板勾选写入的目标才带 id
        （`target_ref()` 给它们造了 `conv:` / `uid:` 前缀的 key，永远不会落在名字表里），
        所以手写的名字目标走的还是原来的名字链路，行为不变。

        带 id 的目标**不给子串兜底**：名字会改、会重名，子串兜底会把「小明」判给「小明明」，
        而 id 对不上就说明不是同一个人 —— 宁可报「没找到」，也不能发给别人。
        """
        by_conv = by_conv or {}
        by_uid = by_uid or {}

        conv_id = str(item.get("conv_id") or "")
        if conv_id and conv_id in by_conv:
            return by_conv[conv_id], "conv_id"

        for field in ("uid", "sec_uid"):
            val = str(item.get(field) or "")
            if val and val in by_uid:
                return by_uid[val], field

        order = ("remark", "nickname", "douyin_id", "uid", "sec_uid", "title")
        for field in order:
            val = item.get(field)
            if not val:
                continue
            n = _norm(val)
            if n and n in pending:
                return n, field
        # 子串兜底：目标出现在显示名里（只对手写的名字目标生效）。
        # ★ 只认「**整段**相等」，不做任意子串：任意子串会把目标「小明」判给好友
        #   「小明明」（本函数 docstring 自己举的例子），而发错人是收不回来的。
        #   分段按空白与常见标点切，所以「老王」照样命中「老王（同事）」；切不中的
        #   手写目标会在扫描结果里报「没找到」（看得见），比"发给了别人、账本还
        #   记成功"（看不见）强。
        joined = " ".join(_norm(item.get(f) or "") for f in
                          ("display", "title", "nickname", "remark"))
        segs = {s for s in (_norm(part) for part in _FUZZY_SPLIT.split(joined)) if s}
        # 昵称两端的 emoji/符号不算数（「🌈LBX🌈」 这种），中间的一个不动
        segs |= {b for b in (_bare(s) for s in segs) if b}
        hits = [n for n in pending
                if n and not n.startswith(("conv:", "uid:")) and n in segs]
        if len(hits) > 1:
            # 兑现 strict_verify 的契约：一个会话同时命中多个目标 → 歧义。
            # `verify_selection` 的第一道闸就是判 `ambiguous`，但在这之前全仓没有
            # 任何生产代码会置位它（只有测试手工构造过），那道闸一直是死的。
            item["ambiguous"] = True
            item["candidates"] = [item.get("conv_id")]
        for n in hits:
            return n, "fuzzy"
        return None, None

    def _snooze(self, ms: int, label: str = "等待") -> None:
        """带计时的等待（异常照常抛出，行为与直接调用一致）。"""
        t0 = time.monotonic()
        try:
            self.page.wait_for_timeout(ms)
        finally:
            _slow_warn(f"{label} {ms}ms", time.monotonic() - t0)

    def _scroll_probe(self):
        t0 = time.monotonic()
        try:
            result = self.page.evaluate(JS_SCROLL_PROBE) or {"found": False}
        except Exception as e:
            logger.warning(
                f"[SCAN] ⚠️ 读滚动状态失败（耗时 {time.monotonic() - t0:.1f}s）：{_brief(e)}"
            )
            return {"found": False}
        _slow_warn("读滚动状态", time.monotonic() - t0)
        return result

    def _scroll_to(self, top):
        t0 = time.monotonic()
        try:
            result = self.page.evaluate(JS_SCROLL_TO, top)
        except Exception as e:
            # 调用方丢掉了返回值，这里是唯一能留痕的地方
            logger.warning(
                f"[SCAN] ⚠️ 滚动到 {top} 失败（耗时 {time.monotonic() - t0:.1f}s）：{_brief(e)}"
            )
            return False
        _slow_warn(f"滚动到 {top}", time.monotonic() - t0)
        return result

    # ---------------------------------------------------- 对外：选中/输入

    MODES = ("focusmouse", "cdp", "jsclick")

    def _select_and_verify(self, item, attempts=None):
        """选中并校验 conv_id。虚拟化下 nth 会漂，所以每轮都重新定位。

        每轮换一种点击方式，并把「当前选中会话」与「网络信号」一起交给
        `core.strict_verify.verify_selection()` 做 **fail-closed** 裁决：
        只有拿到硬证据（选中会话的 conv_id 或反推出的对方 uid 与目标一致）
        才算成功。拿不到证据、或证据指向别人，都继续换方式重试。

        判定细节与失败原因见 `strict_verify`；最近一次的裁决留在
        `self.last_select_verdict` 里供诊断/仪表盘读取。
        """
        want = item.get("conv_id")
        self.last_select_verdict = None      # 每次调用都从干净状态开始
        modes = (("jsclick", "cdp", "mouse") if self._input_mode() == "synth"
                 else ("mouse", "elclick", "jsclick"))
        if attempts is not None:
            modes = modes[:attempts]

        # 网络信号探针：点开会话一定会打 /v1/message/get_by_conversation
        probe = {"n": 0, "hit": False, "t": 0.0}

        def _on_resp(resp):
            try:
                u = resp.url
            except Exception:
                return
            if "get_by_conversation" not in u:
                return
            probe["n"] += 1
            try:
                b = resp.body() or b""
            except Exception:
                b = b""
            if b and want.encode() in b:
                probe["hit"] = True
                probe["t"] = time.time()

        self.page.on("response", _on_resp)
        try:
            for i, mode in enumerate(modes, 1):
                t0 = time.time()
                idx = self._dom_index_of(want)
                if idx is None:
                    self._scroll_to(item.get("data_index", 0) * ROW_HEIGHT)
                    self.page.wait_for_timeout(1200)
                    idx = self._dom_index_of(want)
                    if idx is None:
                        logger.warning(f"[SEL] 第 {i} 次[{mode}]：定位不到 conv_id={want}")
                        continue
                box_info = None
                try:
                    box_info = self._mouse_select(idx, mode)
                except Exception as e:
                    logger.warning(f"[SEL] 第 {i} 次[{mode}] 点击异常：{_brief(e)}")
                    continue

                cur = None
                for _ in range(12):          # 最多 ~2.4s，出现即退出
                    cur = self._current_conv()
                    if cur and cur.get("convId") == want:
                        break
                    # 网络信号到了只证明"点开的确实是目标"，页面上的选中态可能还没
                    # 跟上；而下面对选中的判据以 conv_id 为准（期望值里带了 want），
                    # 所以**读得出 id 就再等一等**，读不出来才不必干等。
                    if (probe["hit"] and probe["t"] >= t0
                            and not (cur or {}).get("convId")):
                        break
                    self.page.wait_for_timeout(200)
                net = probe["hit"] and probe["t"] >= t0

                # ★ 严格确认（fail-closed）。上游在这里有三条乐观通过：
                #     `not want` / `not cur["convId"]` / 仅网络信号 都算成功，
                #   后果是选中态还停在上一个会话就往那儿打字发送。现在改成
                #   只要拿不到「选中的就是目标」的硬证据（conv_id 或对方 uid），
                #   一律判失败继续换方式重试。
                sv = strict_verify()
                peer = self._peer_uid_for(want, cur, item.get("uid"))
                exp = sv.build_expectation(
                    item.get("display") or item.get("title") or want or "",
                    self_uid=self.self_uid, peer_uid=peer,
                    # ★ 把扫描时读到的目标 conv_id 原样交给判定层：反算要 self_uid，
                    #   而 self_uid 为 None 时 conv_id/peer_uid 两个信号会一起消失。
                    conv_id=want,
                    display=item.get("display") or item.get("title") or "",
                    matched_by=item.get("matched_by") or "conv_id",
                    ambiguous=item.get("ambiguous"),
                    candidates=item.get("candidates"),
                )
                verdict = sv.verify_selection(exp, cur_conv=cur, net_hit=net,
                                             self_uid=self.self_uid)
                self.last_select_verdict = verdict

                if verdict.ok:
                    logger.info(f"[SEL] ✅ 已确认选中 方式={mode} 级别={verdict.level}  "
                                f"网络信号={net}  conv_id={(cur or {}).get('convId')}  "
                                f"title={(cur or {}).get('title')}")
                    return True
                if (cur or {}).get("convId") or net:
                    logger.warning(f"[SEL] 第 {i} 次[{mode}] 证据不足，继续重试："
                                   f"{verdict.reason} —— {verdict.detail}")

                try:
                    diag = self.page.evaluate(JS_SEL_DIAG, {
                        "px": (box_info or {}).get("x", -1),
                        "py": (box_info or {}).get("y", -1),
                    })
                except Exception as e:
                    diag = f"diag失败 {_brief(e)}"
                logger.warning(f"[SEL] 第 {i} 次[{mode}] 未确认  目标={want}  耗时={time.time()-t0:.1f}s")
                logger.warning(f"[SEL]   点击={box_info}")
                logger.warning(f"[SEL]   当前={cur}")
                logger.warning(f"[SEL]   页面={diag}")
        finally:
            try:
                self.page.remove_listener("response", _on_resp)
            except Exception:
                pass
        # 三种方式都没走完一次「观察 → 裁决」（例如每轮点击都抛异常）时，
        # last_select_verdict 会是 None。补一条显式的 fail-closed 结论，
        # 让诊断/仪表盘永远能读到"放弃的理由"，而不是一个空值。
        if self.last_select_verdict is None:
            self.last_select_verdict = strict_verify().Verdict(
                False, strict_verify().LEVEL_NONE, strict_verify().R_NO_EVIDENCE,
                f"{len(modes)} 种点击方式都没能观察到任何选中证据",
                {"conv_id": want, "modes": list(modes)})
        logger.warning(f"[SEL] ❌ 放弃 conv_id={want}（{len(modes)} 种方式都未确认，301={probe['n']}）")
        return False

    def _dom_index_of(self, conv_id):
        """在**当前窗口内**查 conv_id 对应的 DOM 下标。"""
        try:
            rows = self.page.evaluate(JS_COLLECT) or []
        except Exception:
            return None
        for r in rows:
            if r.get("convId") == conv_id:
                return r.get("index")
        return None

    JS_INPUT_HOOK = """(() => {
      if (window.__imevHooked) return;
      window.__imevHooked = true;
      window.__imev = [];
      for (const t of ['pointerdown', 'pointerup', 'mousedown', 'mouseup', 'click']) {
        document.addEventListener(t, e => {
          try {
            const tgt = e.target;
            const item = tgt && tgt.closest ? tgt.closest('[data-e2e="conversation-item"]') : null;
            const all = document.querySelectorAll('[data-e2e="conversation-item"]');
            window.__imev.push({
              t: t, trusted: e.isTrusted,
              tag: tgt ? tgt.tagName : null,
              cls: tgt ? String(tgt.className).slice(0, 60) : null,
              itemIdx: item ? [...all].indexOf(item) : -1,
              xy: [Math.round(e.clientX), Math.round(e.clientY)],
              prevented: e.defaultPrevented,
            });
          } catch (err) { window.__imev.push({t: t, err: String(err)}); }
        }, true);
      }
      return 'hooked:' + String(!!window.__imevHooked);
    })()"""

    def _mouse_select(self, index, mode="mouse"):
        """按指定方式选中第 index 个会话。"""
        try:
            hook_state = self.page.evaluate(self.JS_INPUT_HOOK)
            logger.warning(f"[SEL][HOOK] 安装={hook_state}")
        except Exception as e:
            logger.warning(f"[SEL][HOOK] 安装失败：{_brief(e)}")

        handle = self.page.evaluate_handle(
            "(i) => document.querySelectorAll('[data-e2e=\"conversation-item\"]')[i]", index)
        el = handle.as_element()
        if el is None:
            raise RuntimeError(f"下标 #{index} 上没有会话元素")
        el.scroll_into_view_if_needed()
        box = el.bounding_box()
        if not box:
            raise RuntimeError(f"下标 #{index} 的元素不可见")
        x = box["x"] + box["width"] / 2
        y = box["y"] + min(box["height"] / 2, 26)

        if mode == "elclick":
            el.click()
        elif mode == "focusmouse":
            try:
                self.page.bring_to_front()
            except Exception as e:
                logger.warning(f"[SEL] bring_to_front 失败：{_brief(e)}")
            self.page.mouse.move(x, y)
            self.page.mouse.down()
            self.page.mouse.up()
        elif mode == "jsclick":
            self.page.evaluate(
                "(i) => { const e = document.querySelectorAll('[data-e2e=\"conversation-item\"]')[i];"
                " if(e){ e.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));"
                " e.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));"
                " e.dispatchEvent(new MouseEvent('click',{bubbles:true})); } }", index)
        elif mode == "cdp":
            cdp = self.page.context.new_cdp_session(self.page)
            try:
                for typ, buttons in (("mouseMoved", 0), ("mousePressed", 1), ("mouseReleased", 0)):
                    cdp.send("Input.dispatchMouseEvent", {
                        "type": typ, "x": x, "y": y, "button": "left",
                        "buttons": buttons, "clickCount": 1,
                    })
            finally:
                try:
                    cdp.detach()
                except Exception:
                    pass
        else:
            self.page.mouse.move(x, y)
            self.page.mouse.down()
            self.page.mouse.up()

        self.page.wait_for_timeout(400)
        probe = None
        return {"x": x, "y": y, "box": dict(box), "mode": mode}

    def _input_mode(self):
        """探测真实输入事件能否送达页面。

        抖音在某些环境（本次是 Linux 容器 / 无头）下会在 document 捕获阶段吞掉
        所有 trusted 输入事件：CDP 的 mouse/keyboard 全部石沉大海，页面 0 事件。
        探测一次即可决定后续走 real 还是 synth（JS 合成事件）。
        """
        mode = getattr(self, "_input_mode_cache", None)
        if mode:
            return mode
        try:
            self.page.evaluate(self.JS_INPUT_HOOK)
            self.page.evaluate("() => { window.__imev = []; }")
            self.page.mouse.move(2, 2)
            self.page.wait_for_timeout(300)
            ev = self.page.evaluate("() => (window.__imev || []).splice(0)") or []
            mode = "real" if ev else "synth"
        except Exception:
            mode = "synth"
        self._input_mode_cache = mode
        logger.warning(f"[IM] 输入模式探测结果 = {mode}"
                       + ("（真实输入被页面吞掉，改用 JS 合成事件）" if mode == "synth" else ""))
        return mode

    def _js_click_send(self):
        try:
            return self.page.evaluate(JS_CLICK_SEND)
        except Exception:
            return None

    def _current_conv(self):
        try:
            return self.page.evaluate(JS_CURRENT_CONV)
        except Exception as e:
            logger.warning(f"[SEL] _current_conv 异常：{_brief(e)}")
            return None

    def _peer_uid_for(self, conv_id, cur_conv=None, fallback=None):
        """目标的对方 uid，按可靠性从硬到软取：

        1. 由 conv_id 反推（抖音自己算出来的，最硬）
        2. 扫描阶段 join 到的 `uid`（可能来自 `/im/user/info/`，会滞后）

        ★ 刻意**不取** `cur_conv.get("peerUid")`：那是"当前选中项"自己的字段，而这里
        要的是"目标是谁" —— 拿被判定对象的数据当期望值等于自己判自己。`cur_conv`
        只留给判定层当证据（见 `strict_verify.signal_peer_uid`）。
        """
        return (peer_uid_of(conv_id, self.self_uid)
                or fallback or None)

    def _make_reselect(self, item):
        """给外部一个「重新选中」的抓手：发送失败重试时用。"""
        def reselect():
            return self._select_and_verify(item)
        return reselect

    # ---------------------------------------------------- 对外：输入发送

    def type_and_send(self, hit, text, wait_receipt=True, timeout=20.0,
                      dry_run=DRY_RUN_OFF):
        """给「已选中」的会话输入并发送。

        输入走真实键盘事件（Draft.js 依赖 beforeinput/keydown 序列更新 EditorState，
        直接改 DOM 或只派发 input 事件都不可靠）。拟人节奏由 cloakbrowser humanize 负责，
        这里不重复加延迟。

        `dry_run` 三档（见 `norm_dry_run`）：
        * `off`    —— 正常发送
        * `select` —— 走到「输入前的最后一步」就返回：不输入、不点发送
        * `full`   —— 真的把文本输入进 Draft.js 编辑器、**读回校验**，然后清空编辑器，
                      不点发送。这一档能暴露「编辑器找不到 / 合成输入缺行 / 换行处理错」
                      这类只有真输入才会暴露的问题，而 dry-run 的判定与真跑走的是
                      **同一套严格确认**，所以 full 的结果≈真跑结果（除发送本身）。

        返回 {ok, via, message_id, code, status, conv_id, display, reason, level,
              dry_run, rate_limited, limit}
        """
        if not hit:
            raise ValueError("hit is None")
        if not text:
            raise ValueError("text is empty")

        dry = norm_dry_run(dry_run)
        base = {"conv_id": hit.get("conv_id"), "display": hit.get("display"),
                "dry_run": dry, "rate_limited": False, "limit": None}

        # ⓪ 本轮已经复检确认掉登录：剩下的目标不再试。
        #    每个目标白等 20 秒回执、再重试一次，只是把风控喂大而已（见 recheck_login）。
        if getattr(self, "_login_dead", False):
            logger.warning(f"[SEND] ⏭️ 跳过 {hit.get('display')}：登录已失效"
                           f"（{getattr(self, '_login_dead_reason', '') or 'UNKNOWN'}），"
                           f"本轮不再尝试发送")
            out = dict(base)
            out.update({"ok": False, "via": None, "reason": "login_lost",
                        "login_lost": True,
                        "login_state": getattr(self, "_login_dead_reason", ""),
                        "message_id": None, "code": None, "status": None,
                        "level": "none", "text_len": len(text)})
            return out

        # ① 草稿残留自查：上一条没清干净会导致两条消息串发
        try:
            empty = self.page.evaluate(JS_EDITOR_EMPTY)
        except Exception:
            empty = None
        if empty is False or getattr(self, "_editor_dirty", False):
            # `self._editor_dirty`：本进程记的账 —— 上一次输入有没有清干净。
            # 只看 `JS_EDITOR_EMPTY` 会被骗：Draft 的 state 没变时 DOM 可能是空的。
            dirty = bool(getattr(self, "_editor_dirty", False))
            logger.debug(f"[SEND] ⚠️ {'输入框已有内容（草稿残留）' if empty is False else '上一次输入可能没清干净'}"
                         f"，先清空（empty={empty} dirty={dirty}）")
            cleared, how, left = self._clear_editor(distrust_empty=dirty)
            if not cleared:
                # ★ 不清干净就打字 = 上一条 + 这一条叠成一条发出去。宁可这一条不发
                #   （面板上留一条诚实的失败原因），也不能让朋友收到两遍。
                logger.error(f"[SEND] ❌ 输入框清不掉（how={how} 残留={left!r}）：这一条不发，"
                             f"叠发两条比漏发一条更糟")
                out = dict(base)
                out.update({"ok": False, "via": "none", "reason": "editor_not_cleared",
                            "editor": {"empty": empty, "clear_how": how, "left": left},
                            "message_id": None, "code": None, "status": None,
                            "level": "none", "text_len": len(text)})
                return out
            logger.debug(f"[SEND] 清空输入框 how={how}")

        # ② dry-run=select：确认编辑器可用后立刻收工，一个字符都不输入
        if dry == DRY_RUN_SELECT:
            probe = self._editor_probe()
            ok = probe.get("exists") is True
            reason = "" if ok else "editor_missing"
            logger.info(f"[DRY] 选中已确认，dry-run=select 到此为止"
                        f"（编辑器就绪={probe.get('exists')} 空={probe.get('empty')}）"
                        f" 目标={hit.get('display')}")
            out = dict(base)
            out.update({"ok": ok, "via": "dry-run", "editor": probe,
                        "reason": reason,
                        "message_id": None, "code": None, "status": None,
                        "level": "strong" if ok else "none",
                        "text_len": len(text)})
            return out

        # ③ 输入。用 contenteditable 本体，humanize 的「可编辑」检查能过
        # 两种换行都认（口径见 split_message_lines 的文档）
        lines = split_message_lines(text)
        # ★ 这一行是"换行到底有没有生效"的唯一现场记录：模板里的字面 `\n`
        #   如果在配置层被吃掉（例如 systemd 的 EnvironmentFile 把 `\n` 解析成
        #   裸 `n`），日志里就是"1 行"而不是"3 行" —— 必须一眼看得出来。
        logger.debug(f"[SEND] 待发送文本 {text!r} → {len(lines)} 行：{lines!r}")
        if text != "\n".join(lines):
            logger.debug(f"[SEND] 换行归一：{text!r} → {lines!r}")
        if self._input_mode() == "synth":
            ready = False
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                try:
                    if self.page.evaluate(JS_EDITOR_EXISTS):
                        ready = True
                        break
                except Exception:
                    pass
                self.page.wait_for_timeout(200)
            logger.debug(f"[SEND] 编辑器就绪={ready}")
            # ★ 逐行调用、行间留 250ms：Draft.js 每收一次 beforeinput 都会重渲染
            # contenteditable 并丢掉 DOM 选区，在一个 JS 循环里连插会让第 2 行起
            # 静默 no-op（症状：只有第一行 + 后面全空行）。详见 JS_TYPE_LINE 的注释。
            steps = []
            for i, line in enumerate(lines):
                steps.append(self.page.evaluate(JS_TYPE_LINE, {
                    "text": line,
                    "enter": i != len(lines) - 1,
                }))
                self.page.wait_for_timeout(250)
            try:
                empty_now = self.page.evaluate(JS_EDITOR_EMPTY)
            except Exception:
                empty_now = None
            typed = sum(int(s.get("grew", 0)) for s in steps if isinstance(s, dict))
            want_len = sum(len(l) for l in lines)
            logger.debug(f"[SEND] 合成输入={steps}  已键入={typed}/{want_len}  输入框为空={empty_now}")
            # 换行没插进去（三种退化办法都没让行数变多）：文字会全挤在一行。
            # 不抛异常 —— 宁可发出去一行也不要漏掉这次续火花，但绝不能不说。
            brk_none = sum(1 for s in steps if isinstance(s, dict) and s.get("brk") == "none")
            if brk_none:
                logger.warning(f"[SEND] ⚠️ {brk_none} 处换行没插进去（brk=none）："
                               f"这条消息会挤成一行（模板里写的换行没生效）")
            if typed < want_len:
                logger.warning(f"[SEND] ⚠️ 合成输入缺行：已键入 {typed} 字，应为 {want_len} 字"
                               f"（lines={lines}）")
                if typed == 0:
                    if dry == DRY_RUN_FULL:
                        # dry-run 里不抛：空跑的目的就是**报告**问题而不是炸掉整轮
                        logger.warning("[DRY] 合成输入一行都没进去（dry-run 不发送）")
                    else:
                        raise RuntimeError("合成输入一行都没进去，拒绝发送空消息")
        else:
            editor = self._editor()
            if editor is None:
                if dry == DRY_RUN_FULL:
                    out = dict(base)
                    out.update({"ok": False, "via": "dry-run", "editor": None,
                                "reason": "editor_missing", "message_id": None,
                                "code": None, "status": None, "level": "none",
                                "text_len": len(text)})
                    return out
                raise RuntimeError("找不到聊天输入框")
            editor.click()
            for i, line in enumerate(lines):
                if line:
                    self.page.keyboard.type(line)
                if i != len(lines) - 1:
                    self.page.keyboard.press("Shift+Enter")

        # 从这一刻起编辑器里有我们写的字 —— 不论后面发没发成功都得记着：Draft 的
        # "DOM 看着空了、state 还留着"只有本进程能记账（见 _clear_editor）。
        self._editor_dirty = True

        # ④ dry-run=full：读回校验 + 清空编辑器，不点发送
        if dry == DRY_RUN_FULL:
            probe = self._editor_probe()
            match, got = _readback_matches(lines, probe.get("text"))
            cleared, clear_how, clear_left = self._clear_editor(distrust_empty=True, tries=3)
            ok = bool(probe.get("exists") is True and match and cleared)
            if not match:
                reason = "editor_text_mismatch"
            elif not cleared:
                reason = "editor_not_cleared"
            elif probe.get("exists") is not True:
                reason = "editor_missing"
            else:
                reason = ""
            logger.info(f"[DRY] 输入校验 dry-run=full：读回={len(got)}字/{len(line_list(got))}行 "
                        f"期望={len(lines)}行 匹配={match} 已清空={cleared}"
                        f"（清空方式={clear_how}）"
                        f"{'（innerText 取不到，退回 textContent，换行判定不可靠）' if probe.get('fallback') else ''}"
                        f" → {'✅' if ok else '❌ ' + reason}")
            if not match:
                logger.warning(f"[DRY] 读回不一致：期望 {lines!r} / 实际 {line_list(got)!r}"
                               f"（块数={probe.get('blocks')} <br>数={probe.get('brs')}）")
            if not cleared:
                logger.warning(f"[DRY] 编辑器清不掉（方式={clear_how} 残留={clear_left!r}）："
                               f"Draft 的 EditorState 没跟着变，下一次输入旧文本会回来")
            out = dict(base)
            out.update({"ok": ok, "via": "dry-run", "editor": probe,
                        "typed_text": got[:200], "match": match, "cleared": cleared,
                        "clear_how": clear_how, "reason": reason, "message_id": None,
                        "code": None, "status": None, "level": "strong" if ok else "none",
                        "text_len": len(text)})
            return out

        # ④' 真跑前**读回一次**（与 dry-run=full 同一套判据），但只在有**强证据**
        #     时才拦下这一条：判据是"同一条消息出现了两遍"（`_residue_echo`）——
        #     那是上一次输入没清干净、旧文本在重渲染里回来的特征，发出去朋友会看到
        #     两遍。刻意不拿"读回 ≠ 期望"当拦阻条件：排版差异误判的代价是整天不发。
        pre = self._editor_probe()
        echo, echo_n = _residue_echo(lines, pre.get("text"))
        if echo:
            logger.error(f"[SEND] ❌ 输入框里有上一条的残留（首行出现 {echo_n} 遍）：这一条不发"
                         f" —— 发出去会叠成两条。读回={line_list(pre.get('text'))!r}")
            cleared, clear_how, clear_left = self._clear_editor(distrust_empty=True, tries=3)
            logger.error(f"[SEND] 残留清理 how={clear_how} 已清空={cleared}"
                         + (f" 残留={clear_left!r}" if not cleared else ""))
            out = dict(base)
            out.update({"ok": False, "via": "none", "reason": "editor_residue",
                        "editor": pre, "echo": echo_n, "clear_how": clear_how,
                        "message_id": None, "code": None, "status": None,
                        "level": "none", "text_len": len(text)})
            return out

        sends_before = len(self.mon.sends)
        msg_before = self._msg_state()

        # ⑤ 发送：按钮优先（有内容时变红可点），退化到回车
        how = self._js_click_send() if self._input_mode() == 'synth' else self._click_send()
        logger.debug(f"[SEND] 发送方式={how}  conv_id={hit.get('conv_id')}")

        if not wait_receipt:
            return dict(base, ok=True, via=how)

        rc = self._wait_receipt(sends_before, msg_before, text, timeout, hit=hit)
        if rc["http"]:
            h = rc["http"]
            # 一律用 .get()：这条日志在**消息已经发出去之后**才执行，
            # 为了打印一行日志炸掉整轮是最亏的买卖。
            (logger.info if h.get("ok") else logger.warning)(
                f"[SEND] {'✅' if h.get('ok') else '❌'} HTTP 回执 code={h.get('code')} "
                f'status="{h.get("status")}" message_id={h.get("message_id") or "-"}')
        if rc["dom"]:
            logger.debug(f"[SEND] ✅ DOM 确认：消息数 {msg_before.get('count', '?')} "
                         f"→ {rc['dom']['count']}，最后一条是自己发的")
        if not rc["ok"]:
            reason = (rc.get("verdict").reason if rc.get("verdict") else "unknown")
            logger.warning(f"[SEND] ⚠️ {timeout:.0f}s 内没拿到可信回执"
                           f"（原因={reason}，可能走 WS 通道，或发送被拦）")
            # 没有回执 ≠ 发送失败：也可能登录态已经没了（页面还是 IM 的样子）。
            # 复检一次，把「掉登录」从「发送失败」里分出来 —— 不做这一步，掉登录
            # 会被记成"跑了但 4 个目标失败"，整轮照样按 ok 记账，当天不再重试，
            # 面板上也没有任何提示（2026-10-09 wheat 的真实事故）。
            lg = self.recheck_login()
            if str((lg or {}).get("state") or "") != "LOGGED_IN":
                logger.error(f"[SEND] ⛔ 放弃发送：登录已失效"
                             f"（{(lg or {}).get('state')}），本轮剩余目标不再尝试")
                out = dict(base)
                out.update({"ok": False, "via": None, "reason": "login_lost",
                            "login_lost": True,
                            "login_state": (lg or {}).get("state"),
                            "message_id": None, "code": None, "status": None,
                            "level": "none", "text_len": len(text)})
                return out
        else:
            # 消息确实发出去了 = 抖音那边把输入框清空了，本进程的账跟着清；
            # 没拿到回执时**故意保留** dirty 标记 —— 那正是重试前必须清残留的场合
            self._editor_dirty = False
        # ⑥ 熔断：DOM 提示文案 + 本轮所有发送回执，两个来源一起看（回执优先）
        limit_hit = self.check_limit(receipts=rc.get("receipts") or [])
        if limit_hit:
            logger.error(f"[LIMIT] ⛔ 命中风控/限流信号：{limit_hit}")
        out = {
            "ok": rc["ok"],
            "via": ("http+dom" if rc["http"] and rc["dom"] else
                    "http" if rc["http"] else "dom" if rc["dom"] else None),
            "message_id": (rc["http"] or {}).get("message_id"),
            "code": (rc["http"] or {}).get("code"),
            "status": (rc["http"] or {}).get("status"),
            "conv_id": hit.get("conv_id"),
            "display": hit.get("display"),
            "reason": (rc.get("verdict").reason if rc.get("verdict") else ""),
            "level": (rc.get("verdict").level if rc.get("verdict") else ""),
            "dry_run": dry,
            "rate_limited": bool(limit_hit),
            "limit": limit_hit,
        }
        return out

    def _clear_editor(self, tries=2, settle_ms=250, distrust_empty=False):
        """清空输入框，并**确认它真的空了**（Draft 的 state，不只是 DOM）。

        返回 `(cleared, how, left)`：`how` 是真正奏效的那条路（见 `JS_EDITOR_CLEAR`：
        `key`/`cmd`/`already-empty`/`none`），全都没成时 `how='none'`、
        `left` 是残留文本的开头。

        ★ 为什么要"再来一遍 + 复查"：`execCommand('delete')` 只抹 DOM、不动 Draft
        的 EditorState（2026-10-03 真机 dry-run=full 实测），于是清空当场看着成功、
        判空也通过，可下一次输入 React 重渲染时旧文本又冒出来 —— 两条消息叠着发。
        所以清完要等一小会儿再读一次，读到的不是空就再清一遍，最多 `tries` 轮。

        ★ `distrust_empty`：调用方**知道**"编辑器不久前刚被打过字、而且没清干净"
        （上一次判定失败留下的）。此时"看着是空的"不能直接信 —— DOM 被抹掉了、
        state 还留着，正是上面那个坑的现场。判不出来的东西只能逼它现形：插一个
        字符会走 Draft 的 state 更新，藏在 state 里的残留跟着重渲染出来，下一轮
        就一起清掉了（没残留时也只是把刚插的那个字符删掉，净效果一样是空的）。
        """
        how, left = "none", ""
        probe_left = bool(distrust_empty)
        for _ in range(max(1, int(tries))):
            try:
                r = self.page.evaluate(JS_EDITOR_CLEAR)
            except Exception as e:
                logger.debug(f"[SEND] 清空输入框失败：{_brief(e)}")
                return False, "error", left
            if isinstance(r, dict):
                how = str(r.get("how") or "unknown")
                left = str(r.get("left") or "")
            self.page.wait_for_timeout(settle_ms)
            if self._editor_probe().get("empty") is True:
                if not probe_left:
                    self._editor_dirty = False
                    return True, how, ""
                probe_left = False
                logger.debug(f"[SEND] 清空输入框 how={how}，但这条消息刚才打过字："
                             f"插一个字符逼 Draft 重渲染，确认没有藏在 state 里的残留")
                try:
                    self.page.evaluate(JS_TYPE_LINE, {"text": "."})
                except Exception:
                    pass
                self.page.wait_for_timeout(settle_ms)
                continue
            if how in ("key", "cmd"):
                logger.warning(f"[SEND] 清空输入框：{how} 这一路当场看着空了，"
                               f"{settle_ms}ms 后文本又回来了（Draft 的 state 没跟着变）")
        return False, how, left

    def _editor_probe(self):
        """编辑器三连探：存在 / 为空 / 当前文本。任一步失败该字段留 None。

        刻意全部走 `page.evaluate`（而不是 locator），因为 dry-run 的判定必须在
        「页面不可交互」的退化情况下也能给出**可解释**的结果，而不是抛异常。
        """
        out = {"exists": None, "empty": None, "text": None, "len": None}
        try:
            out["exists"] = bool(self.page.evaluate(JS_EDITOR_EXISTS))
        except Exception:
            pass
        try:
            v = self.page.evaluate(JS_EDITOR_TEXT)
            if isinstance(v, dict):
                out["text"] = v.get("text")
                out["len"] = v.get("len")
        except Exception:
            pass
        try:
            out["empty"] = self.page.evaluate(JS_EDITOR_EMPTY)
        except Exception:
            pass
        return out

    def limit_texts(self):
        """采集页面上**可见的提示类文案**（限流/风控信号源之一）。失败返回 []。"""
        try:
            v = self.page.evaluate(JS_LIMIT_TEXT)
        except Exception as e:
            logger.debug(f"[LIMIT] 提示文案采集失败：{e}")
            return []
        return [str(x) for x in v] if isinstance(v, list) else []

    def check_limit(self, receipts=()):
        """DOM 文案 + 发送回执一起扫 → 命中信息或 None。

        两个来源都要看的理由见 `core/limit_guard.py` 的模块注释；关键词表与
        冷却状态机也都在那里（本方法只负责"取证据"）。
        """
        try:
            from core import limit_guard as lg
        except Exception:
            return None
        return lg.detect(receipts=receipts, dom_texts=self.limit_texts())


    def _editor(self):
        for sel in ('[data-e2e="msg-input"] .public-DraftEditor-content',
                    '.DraftEditor-root [contenteditable="true"]',
                    '.messageEditorimChatEditorContainer'):
            try:
                loc = self.page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
            except Exception:
                continue
        return None

    def _msg_state(self):
        """消息区状态快照。

        `JS_MSG_STATE` 本身只数气泡（**全局** `[data-e2e="msg-item-content"]` 数量，
        不分会话），这里补一个 `convId`：回执判定必须知道"现在看的是哪个会话"，
        否则区分不了「发到目标但气泡没涨」和「发到别人那儿去了」。
        convId 复用「当前选中会话」的读数，两者必须是同一个时刻的语义。
        """
        try:
            st = self.page.evaluate(JS_MSG_STATE) or {}
        except Exception:
            return {}
        try:
            cur = self._current_conv()
        except Exception:
            cur = None
        st["convId"] = (cur or {}).get("convId")
        st["title"] = (cur or {}).get("title")
        return st

    def _click_send(self):
        try:
            btn = self.page.locator(SEL_SEND_BTN_READY).first
            if btn.count() > 0 and btn.is_visible():
                btn.click()
                return "button"
        except Exception:
            pass
        self.page.keyboard.press("Enter")
        return "enter"

    def _wait_receipt(self, sends_before, msg_before, text, timeout, hit=None):
        """回执双确认：HTTP 监听（权威）+ DOM（WS 通道下补位）。

        ★ 关键改动：DOM 侧不再只看「气泡数变多」。虚拟列表不重渲染时气泡数
        常常不涨，而上游把这种情况当成失败；反过来它又会因为**任何一条** ok 的
        发送响应就判成功——而那个响应里根本没有收件人字段（`decode_send_resp`
        只解 code/status/message_id）。所以这里改成：

        * DOM 侧同时采 `convId`，明确指向**别的会话**时判「发错人」（最严重）；
        * 气泡数没涨只算「没确认」，不足以反证；
        * 最终裁决交给 `strict_verify.verify_receipt()`，它会把两侧证据
          合起来给出 ok / level / reason。
        """
        t0 = time.time()
        http = dom = None
        last_http = None          # 最后一条响应（可能是失败的，用来解释原因）
        dom_conv_off = False
        want = (hit or {}).get("conv_id")
        head = (text or "")[:8]

        while time.time() - t0 < timeout:
            for rec in self.mon.sends[sends_before:]:
                last_http = rec
                if http is None and rec.get("ok"):
                    http = rec
            if dom is None:
                st = self._msg_state()
                st_conv = st.get("convId")
                if want and st_conv and st_conv != want:
                    # 发送后当前会话变成了别人 → 立刻停，这比"没发出去"严重
                    dom_conv_off = True
                    break
                if (st.get("count", 0) > msg_before.get("count", 0)
                        and st.get("lastFromMe")):
                    if not head or (st.get("lastText") or "").startswith(head):
                        dom = st
            if http and dom:
                break
            if http and time.time() - t0 > 1.0:
                break
            if dom and time.time() - t0 > 2.0:
                break
            self.page.wait_for_timeout(150)

        # 组装给 strict_verify 的观测快照
        after = dict(dom or {})
        if dom_conv_off:
            try:
                after = dict(self._msg_state() or {})
            except Exception:
                after = {}
        if not after.get("convId"):
            # DOM 没给出 convId 时**不要**凭空填目标 id，否则会把"看不到"
            # 伪装成"确认了目标" —— 这正是上游的漏洞形态。但也不能留一个
            # 空的 convId：`verify_receipt` 只有在真拿到 id 时才敢判"发错人"，
            # 看不到就是看不到，让判定落到"没确认"上。
            after.setdefault("count", (dom or {}).get("count", 0))
            after.setdefault("lastFromMe", (dom or {}).get("lastFromMe", False))
            after["convId"] = None

        receipts = list(self.mon.sends[sends_before:])
        if last_http is not None and not any(r is last_http for r in receipts):
            receipts.append(last_http)

        sv = strict_verify()
        exp = sv.build_expectation(
            (hit or {}).get("display") or want or "",
            self_uid=self.self_uid,
            peer_uid=self._peer_uid_for(want, hit, (hit or {}).get("uid")),
            display=(hit or {}).get("display") or "",
            matched_by="conv_id",
        )
        verdict = sv.verify_receipt(exp, before=msg_before, after=after,
                                    receipts=receipts, self_uid=self.self_uid)
        self.last_receipt_verdict = verdict

        dom_state = dom or (after if after.get("count") else None)
        return {"ok": verdict.ok, "http": http, "dom": dom_state,
                "verdict": verdict, "dom_conv_off": dom_conv_off,
                # receipts 交给调用方做风控扫描：服务端回的 code/status 是
                # **机器可读**的限流信号，比在页面上找"操作频繁"四个字可靠得多
                "receipts": receipts}

    # -------------------------------------------------------------- 其它

    def fold_groups(self):
        """折叠组 / 陌生人组是独立容器，主容器扫不到。这里列出它们的条目数，
        供调用方判断「找不到」是否可能因为目标被折叠。"""
        out = {}
        for name, sel in (("fold", SEL_LIST_FOLD), ("stranger", SEL_LIST_STRANGER)):
            try:
                out[name] = self.page.evaluate(
                    "(s) => document.querySelectorAll(s).length", sel)
            except Exception:
                out[name] = None
        return out

    def detach(self):
        """摘钩子。"""
        if self._detached:
            return
        try:
            self.mon.detach()
        except Exception:
            pass
        try:
            self.page.remove_listener("response", self._on_resp_login_lost)
        except Exception:
            pass
        self._detached = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.detach()
        return False
