(() => {
  const root = document.documentElement;
  const storageKey = "sparkflow-theme";

  const storedTheme = () => {
    try {
      return localStorage.getItem(storageKey);
    } catch {
      return null;
    }
  };

  const applyTheme = (theme) => {
    const value = theme === "light" ? "light" : "dark";
    root.dataset.theme = value;
    root.style.colorScheme = value;
    try {
      localStorage.setItem(storageKey, value);
    } catch {
      // The active page can still switch themes when storage is unavailable.
    }
  };

  applyTheme(storedTheme() || "dark");
  document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      applyTheme(root.dataset.theme === "light" ? "dark" : "light");
    });
  });
})();

(() => {
  const body = document.body;
  document.querySelectorAll("[data-nav-toggle]").forEach((button) => {
    button.addEventListener("click", () => body.classList.add("nav-open"));
  });
  document.querySelectorAll("[data-nav-close]").forEach((button) => {
    button.addEventListener("click", () => body.classList.remove("nav-open"));
  });
  document.querySelectorAll(".nav-item").forEach((link) => {
    link.addEventListener("click", () => body.classList.remove("nav-open"));
  });
})();

(() => {
  const dialog = document.getElementById("confirm-dialog");
  if (!dialog) return;
  const title = document.getElementById("confirm-title");
  const message = document.getElementById("confirm-message");
  const accept = dialog.querySelector("[data-confirm-accept]");
  const cancel = dialog.querySelector("[data-confirm-cancel]");
  let pendingForm = null;
  let pendingLink = "";
  let pendingButton = null;

  // 真正执行被拦下来的那个动作（表单提交 / 跳转 / 再点一次按钮）
  const runPending = () => {
    const form = pendingForm;
    const href = pendingLink;
    const button = pendingButton;
    pendingForm = null;
    pendingLink = "";
    pendingButton = null;
    if (form) {
      HTMLFormElement.prototype.submit.call(form);
    } else if (href) {
      window.location.assign(href);
    } else if (button) {
      button.dataset.confirmApproved = "1";
      button.click();
    }
  };

  const clearPending = () => {
    pendingForm = null;
    pendingLink = "";
    pendingButton = null;
  };

  const openDialog = (node) => {
    const source = node.closest("[data-confirm]") || node;
    title.textContent = source.dataset.confirmTitle || "确认操作";
    message.textContent =
      source.dataset.confirm ||
      "该操作会立即影响续火花任务，请确认是否继续。";
    accept.textContent = source.dataset.confirmAccept || "确认执行";
    accept.className =
      source.dataset.confirmTone === "primary"
        ? "button button-primary"
        : "button button-danger";
    // 老浏览器没有 <dialog>.showModal：退回原生 confirm，
    // 绝不能因为弹窗打不开就把提交吞掉（那样按钮就成了"点了没反应"）。
    if (typeof dialog.showModal !== "function") {
      if (window.confirm(`${title.textContent}\n\n${message.textContent}`)) {
        runPending();
      } else {
        clearPending();
      }
      return;
    }
    try {
      dialog.showModal();
    } catch (err) {
      if (window.confirm(`${title.textContent}\n\n${message.textContent}`)) {
        runPending();
      } else {
        clearPending();
      }
    }
  };

  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      pendingForm = form;
      pendingLink = "";
      pendingButton = null;
      openDialog(form);
    });
  });

  document.querySelectorAll("a[data-confirm]").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      pendingForm = null;
      pendingLink = link.href;
      pendingButton = null;
      openDialog(link);
    });
  });

  document.querySelectorAll("button[data-confirm]").forEach((button) => {
    button.addEventListener(
      "click",
      (event) => {
        if (button.dataset.confirmApproved === "1") {
          delete button.dataset.confirmApproved;
          return;
        }
        event.preventDefault();
        event.stopImmediatePropagation();
        pendingForm = null;
        pendingLink = "";
        pendingButton = button;
        openDialog(button);
      },
      true,
    );
  });

  cancel.addEventListener("click", () => {
    clearPending();
    dialog.close();
  });

  accept.addEventListener("click", () => {
    dialog.close();
    runPending();
  });

  dialog.addEventListener("cancel", clearPending);
})();

(() => {
  document.querySelectorAll("[data-segment-group]").forEach((group) => {
    const buttons = [...group.querySelectorAll("[data-segment-target]")];
    const owner = group.closest("[data-segment-owner]") || document;
    const panels = [...owner.querySelectorAll("[data-segment-panel]")];
    const activate = (name) => {
      buttons.forEach((button) => {
        const active = button.dataset.segmentTarget === name;
        button.classList.toggle("active", active);
        button.setAttribute("aria-selected", active ? "true" : "false");
      });
      panels.forEach((panel) => {
        panel.hidden = panel.dataset.segmentPanel !== name;
      });
    };
    buttons.forEach((button) => {
      button.addEventListener("click", () =>
        activate(button.dataset.segmentTarget),
      );
    });
    const initial =
      buttons.find((button) => button.classList.contains("active")) ||
      buttons[0];
    if (initial) activate(initial.dataset.segmentTarget);
  });
})();

(() => {
  const overviewRoots = document.querySelectorAll("[data-overview-root]");
  if (!overviewRoots.length) return;
  let previousRunning = null;
  let timer = null;

  const formatTime = (raw) => {
    if (!raw) return "-";
    const parsed = new Date(raw);
    if (Number.isNaN(parsed.getTime())) return raw;
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(parsed);
  };

  const setText = (selector, value) => {
    document.querySelectorAll(selector).forEach((node) => {
      node.textContent = String(value ?? "");
    });
  };

  const updateTaskBanner = (task) => {
    document.querySelectorAll("[data-task-banner]").forEach((banner) => {
      banner.className = "status-banner";
      if (task.running) {
        banner.classList.add("warning");
        banner.querySelector("[data-task-text]").textContent =
          `发送任务运行中，已运行约 ${task.ageSeconds || 0} 秒`;
      } else if (task.stale) {
        banner.classList.add("info");
        banner.querySelector("[data-task-text]").textContent =
          "检测到过期任务锁，下次启动任务时会自动清理";
      } else {
        banner.classList.add("success");
        banner.querySelector("[data-task-text]").textContent =
          "当前没有发送任务运行";
      }
    });
  };

  const updateAccounts = (accounts) => {
    accounts.forEach((account) => {
      const selector = `[data-account-overview="${CSS.escape(account.uniqueId)}"]`;
      document.querySelectorAll(selector).forEach((row) => {
        // 配色按 stateCss 来（"orphan" 这类状态没有自己的配色规则，服务端也是这么渲染的）
        row.dataset.accountState = account.stateCss || account.state;
        row.querySelectorAll("[data-account-confirmed]").forEach((node) => {
          node.textContent = account.confirmed;
        });
        row.querySelectorAll("[data-account-attention]").forEach((node) => {
          node.textContent = account.attention;
        });
        row.querySelectorAll("[data-account-pending]").forEach((node) => {
          node.textContent = account.pending;
        });
        const strongCount = account.confirmed || 0;
        const weakCount = (account.pageEcho || 0) + (account.receiptOnly || 0);
        const accountTotal = account.total || 0;
        // Multiply before dividing so this matches the template's half-up maths,
        // and derive the weak segment as the remainder so the two segments can
        // never add up to more than 100%.
        const strongPct = accountTotal
          ? Math.round((strongCount * 100) / accountTotal)
          : 0;
        const sentPct = accountTotal
          ? Math.round(((strongCount + weakCount) * 100) / accountTotal)
          : 0;
        const weakPct = sentPct - strongPct;
        row.querySelectorAll("[data-account-progress]").forEach((node) => {
          node.style.width = `${strongPct}%`;
        });
        row.querySelectorAll("[data-account-progress-echo]").forEach((node) => {
          node.style.width = `${weakPct}%`;
        });
        row.querySelectorAll("[data-account-progress-text]").forEach((node) => {
          // 与服务端模板保持同一句话：空跑必须标出来，不然轮询一刷就看不出来了
          const dry =
            account.dryRun && account.dryRun !== "off"
              ? ` · 上一轮是空跑（${account.dryRunText || account.dryRun}）`
              : "";
          node.textContent = `已发送 ${strongCount + weakCount}/${accountTotal} · 强确认 ${strongCount}${dry}`;
        });
      });
    });
  };

  const updateActions = (summary, running) => {
    const counts = {
      attention: summary.attention,
      pending: summary.pending + summary.unprocessed,
      total: summary.total,
    };
    document.querySelectorAll("[data-action-count-source]").forEach((button) => {
      const count = counts[button.dataset.actionCountSource] || 0;
      button.disabled = running || count <= 0;
      const countNode = button.querySelector("[data-action-count]");
      if (countNode) countNode.textContent = count;
    });
    document.querySelectorAll("[data-disable-while-running]").forEach((button) => {
      if (!button.dataset.actionCountSource) {
        button.disabled = running;
      }
    });
  };

  const render = (data) => {
    const summary = data.summary || {};
    const task = data.task || {};
    updateTaskBanner(task);
    setText("[data-overview-value='total']", summary.total || 0);
    setText("[data-overview-value='confirmed']", summary.confirmed || 0);
    setText(
      "[data-overview-value='sentTotal']",
      (summary.confirmed || 0) + (summary.pageEcho || 0),
    );
    setText("[data-overview-value='pageEcho']", summary.pageEcho || 0);
    setText("[data-overview-value='receiptOnly']", summary.receiptOnly || 0);
    setText("[data-overview-value='attention']", summary.attention || 0);
    setText(
      "[data-overview-value='pending']",
      (summary.pending || 0) + (summary.unprocessed || 0),
    );
    setText("[data-overview-value='remaining']", summary.remaining || 0);
    const strongCount = summary.confirmed || 0;
    const weakCount = (summary.pageEcho || 0) + (summary.receiptOnly || 0);
    const totalCount = summary.total || 0;
    // Same half-up maths and remainder rule as the server-rendered template.
    const strongPct = totalCount
      ? Math.round((strongCount * 100) / totalCount)
      : 0;
    const sentPct = totalCount
      ? Math.round(((strongCount + weakCount) * 100) / totalCount)
      : 0;
    const weakPct = sentPct - strongPct;
    setText(
      "[data-overview-value='progress']",
      `${strongCount + weakCount}/${totalCount}`,
    );
    setText(
      "[data-overview-value='progressPercent']",
      `${sentPct}%`,
    );
    setText("[data-overview-value='weakSent']", weakCount);
    // 今日进度条：和服务端渲染的宽度算法一致（强确认橙段 + 弱证据黄段）。
    document.querySelectorAll("[data-overview-bar-strong]").forEach((node) => {
      node.style.width = `${strongPct}%`;
    });
    document.querySelectorAll("[data-overview-bar-echo]").forEach((node) => {
      node.style.width = `${weakPct}%`;
    });
    setText(
      "[data-overview-value='lastConfirmedAt']",
      formatTime(summary.lastConfirmedAt),
    );
    // 这一格的说明必须跟着值一起换：值显示"-"却说"通过双确认的目标"，等于自己打脸。
    setText(
      "[data-overview-help='lastConfirmedAt']",
      summary.lastConfirmedAt
        ? "上一轮通过会话选择 + 回执双确认的目标"
        : summary.lastWeakAt
          ? "上一轮只有弱证据（拿到页面回显，没拿到回执），不算强确认"
          : "上一轮没有确认成功的目标",
    );
    // 定时器没启用时，服务端渲染的是"不会自动跑"；轮询也必须说同一句话，
    // 不然页面每 2 秒就被前端改回一个骗人的时间点。
    const schedule = data.schedule || {};
    const armed = schedule.armed !== false;
    // 派发器模式（各账号按自己的发送时间分开跑）：pending 讲的是"上一轮还剩几个
    // 目标没走到"，拿它当"下次触发还有活干"就会谎报 —— 那时候真话是
    // schedule.nextTriggerText（"下一个 wheat：今天 17:20" / "今天该发的都发完了"），
    // 服务端首屏写的也是它，轮询不能把它改回一个光秃秃的时间。
    const dueText = schedule.nextTriggerText || "";
    setText(
      "[data-overview-value='nextTriggerAt']",
      armed
        ? schedule.dispatcher && dueText
          ? dueText
          : formatTime(schedule.nextTriggerAt)
        : "不会自动跑",
    );
    setText(
      "[data-overview-value='scheduleLabel']",
      schedule.label || "-",
    );
    const remainingForNext = schedule.remainingTargets || 0;
    setText(
      "[data-overview-value='nextTriggerHint']",
      !armed
        ? "只能手动点「立即运行」"
        : schedule.dispatcher
          ? (dueText ? `按各账号自己的时间发：${dueText}` : "今天该发的都发完了")
          : schedule.hasWorkNextTrigger
            ? `仍有 ${remainingForNext} 个待发送目标`
            : "当前没有待发送目标，下一次触发不会实际发送",
    );
    updateAccounts(data.accounts || []);
    updateActions(summary, Boolean(task.running));

    if (previousRunning === true && !task.running) {
      document
        .querySelectorAll("[data-refresh-notice]")
        .forEach((node) => node.classList.add("visible"));
    }
    previousRunning = Boolean(task.running);
    document.querySelectorAll("[data-overview-live-state]").forEach((node) => {
      node.textContent = "实时";
      node.classList.remove("poll-stale");
    });
  };

  const refresh = async () => {
    if (document.visibilityState !== "visible") return;
    try {
      const response = await fetch("/api/ops/overview", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      render(await response.json());
    } catch {
      document.querySelectorAll("[data-overview-live-state]").forEach((node) => {
        node.textContent = "更新延迟";
        node.classList.add("poll-stale");
      });
    }
  };

  document.querySelectorAll("[data-refresh-page]").forEach((button) => {
    button.addEventListener("click", () => window.location.reload());
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refresh();
  });
  refresh();
  timer = window.setInterval(refresh, 10000);
  window.addEventListener("pagehide", () => window.clearInterval(timer));
})();

// ---------------------------------------------------------------- 融合补充
// 以下两段是 C（DouYinSparkFlow）独有的能力，B 的分段控件与轮询里没有对应实现：
//   1) dry-run 档位：B 的分段控件只切换面板显隐，这里把它同步进表单；
//   2) 熔断冷却 / 禁跑窗口的倒计时。
// 其余部分（主题、移动端导航、确认框、分段控件、10 秒总览轮询）与 B 原版一致。

(() => {
  const group = document.querySelector("[data-dry-run-group]");
  if (!group) return;
  const input = document.getElementById("dry-run-value");
  const submit = document.getElementById("run-now-submit");
  const apply = () => {
    const active = group.querySelector("[data-segment-target].active");
    const raw = active ? active.dataset.segmentTarget : "off";
    // 挂点是 dry-off / dry-select / dry-full（同一个前缀也用来配对下面的说明面板），
    // 但**提交给服务端**的必须是光秃秃的 off / select / full —— 否则 400。
    const level = raw.replace(/^dry-/, "");
    if (input) input.value = level;
    if (submit) {
      // 真实发送是破坏性操作，用危险色；空跑用柔和色。文案跟着变，
      // 避免出现"按钮写着发送、其实是空跑"这种骗人的界面。
      const real = level === "off";
      submit.className = real ? "button button-danger" : "button button-soft";
      const label = submit.querySelector("[data-run-now-label]");
      if (label) label.textContent = real ? "立即发送" : "开始空跑";
    }
  };
  group.addEventListener("click", () => window.setTimeout(apply, 0));
  apply();
})();

(() => {
  const nodes = [...document.querySelectorAll("[data-countdown-until]")];
  if (!nodes.length) return;
  const tick = () => {
    const now = Date.now();
    nodes.forEach((node) => {
      const until = Date.parse(node.dataset.countdownUntil || "");
      const out = node.querySelector("[data-countdown-text]");
      if (!out || Number.isNaN(until)) return;
      let left = Math.max(0, Math.round((until - now) / 1000));
      const hours = Math.floor(left / 3600);
      left -= hours * 3600;
      const minutes = Math.floor(left / 60);
      const seconds = left - minutes * 60;
      out.textContent = hours
        ? `${hours} 小时 ${minutes} 分`
        : minutes
          ? `${minutes} 分 ${seconds} 秒`
          : `${seconds} 秒`;
    });
  };
  tick();
  window.setInterval(tick, 1000);
})();
// B 把图标渲染放在 app.js 最末尾（原件 :1149-1153），而我们只搬了前 378 行，
// 漏了这一句的结果是：页面上 26 个 <i data-lucide> 全是空的、一个图标都不显示。
window.addEventListener("DOMContentLoaded", () => {
  if (window.lucide) {
    window.lucide.createIcons({ attrs: { "aria-hidden": "true" } });
  }
});

// 扫码登录：每 2 秒问一次服务端"登录到哪一步了"，二维码变了就换图。
// 登录成功（服务端写进 .env）之后整页重载一次 —— 那时账号表、Cookie 状态、
// 顶部横幅都会变成新渲染的内容，比在 JS 里拼 HTML 更可靠。
(() => {
  const root = document.querySelector("[data-login-root]");
  if (!root) return;

  const qr = root.querySelector("[data-login-qr]");
  const statusEl = root.querySelector("[data-login-status]");
  const hintEl = root.querySelector("[data-login-hint]");
  const errorEl = root.querySelector("[data-login-error]");
  const qrStatus = root.querySelector("[data-login-qr-status]");
  const startBtn = root.querySelector("[data-login-start]");
  const cancelBtn = root.querySelector("[data-login-cancel]");
  const timerEl = root.querySelector("[data-login-timer]");
  const statePill = document.querySelector("[data-login-state-label]");
  const persistedOnServer = root.dataset.loginPersisted === "1";
  // 二次验证（风控账号）：抖音要一条短信验证码，这块由服务端状态开关。
  const codeWrap = root.querySelector("[data-login-code-wrap]");
  const codeForm = root.querySelector("[data-login-code-form]");
  const codeInput = root.querySelector("[data-login-code-input]");
  const codeStatus = root.querySelector("[data-login-code-status]");
  const shot = root.querySelector("[data-login-shot]");
  const INTERVAL = 2000;
  let lastQrSeq = root.dataset.qrSeq || "0";
  let lastShotSeq = "0";
  let reloading = false;

  const setText = (node, text) => {
    if (!node) return;
    if (text) {
      node.textContent = text;
      node.hidden = false;
    } else {
      node.textContent = "";
      node.hidden = true;
    }
  };

  const apply = (data) => {
    if (!data || !data.ok) return;
    const session = data.session || {};
    root.dataset.loginState = session.state || "";
    if (statePill) statePill.textContent = session.state_label || "";
    setText(statusEl, session.message || "");
    setText(hintEl, session.hint || "");
    if (errorEl) {
      if (session.error) {
        errorEl.replaceChildren();
        const strong = document.createElement("strong");
        strong.textContent = session.error;
        errorEl.appendChild(strong);
        errorEl.hidden = false;
      } else {
        errorEl.hidden = true;
      }
    }
    if (qr) {
      if (session.has_qr) {
        const seq = String(session.qr_seq || "0");
        if (seq !== lastQrSeq) {
          lastQrSeq = seq;
          qr.src = `/ops/login/qr.png?v=${seq}`;
        }
        qr.hidden = false;
        setText(qrStatus, "用抖音 App 扫这张码，然后在手机上确认。");
      } else {
        qr.hidden = true;
        setText(qrStatus, session.active ? "正在获取二维码…" : "点「开始扫码登录」，二维码会显示在这里。");
      }
    }
    if (codeWrap) codeWrap.hidden = !session.need_code;
    if (codeStatus) {
      if (session.code_error) {
        codeStatus.textContent = session.code_error;
        codeStatus.hidden = false;
      } else {
        codeStatus.hidden = true;
      }
    }
    if (shot && session.has_shot) {
      const shotSeq = String(session.shot_seq || "0");
      if (shotSeq !== lastShotSeq) {
        lastShotSeq = shotSeq;
        shot.src = `/ops/login/shot.png?v=${shotSeq}`;
      }
      shot.hidden = false;
    }
    const active = Boolean(session.active);
    if (startBtn) startBtn.disabled = active;
    if (cancelBtn) cancelBtn.disabled = !active;
    if (timerEl) {
      timerEl.textContent = active ? `已进行 ${session.age_seconds || 0} 秒` : "";
    }
    if (session.persist && !persistedOnServer && !reloading) {
      reloading = true;
      window.location.reload();
    }
  };

  const poll = async () => {
    if (document.visibilityState !== "visible") return;
    try {
      const response = await fetch("/api/ops/login", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) return;
      apply(await response.json());
    } catch (error) {
      /* 网络抖动就跳过这一轮，下一次轮询会补上 */
    }
  };

  // 验证码用 fetch 提交：面板不用整页重载，提交结果直接显示在下面一行。
  // 网络抖了就退回普通表单提交（服务端两条路都留着）。
  if (codeForm) {
    codeForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const code = codeInput ? String(codeInput.value || "").trim() : "";
      if (!code) return;
      try {
        const response = await fetch("/ops/login/code", {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/x-www-form-urlencoded",
            Accept: "application/json",
          },
          body: new URLSearchParams({ code }),
        });
        const data = await response.json().catch(() => ({}));
        if (data && data.ok) {
          if (codeInput) codeInput.value = "";
          setText(codeStatus, "验证码已提交，正在等抖音放行…");
        } else {
          setText(codeStatus, (data && data.error) || "验证码没送进去，再试一次");
        }
      } catch (error) {
        codeForm.submit();
      }
    });
  }

  poll();
  window.setInterval(poll, INTERVAL);
})();


// 好友勾选：列表本身由服务端渲染（勾没勾一眼可见），这里只做两件事 ——
// ①本地搜索过滤 + 勾选计数；②读取进行中时每 2 秒问一次进度，读完整页重载，
// 让服务端把新读到的会话补进列表、并把 .env 里已保存的目标勾上。
(() => {
  const roots = Array.from(document.querySelectorAll("[data-friend-picker]"));

  const wirePicker = (root) => {
    const list = root.querySelector("[data-friend-list]");
    const countEl = root.querySelector("[data-friend-count]");
    const search = root.querySelector("[data-friend-search]");
    const boxes = () => (list ? Array.from(list.querySelectorAll('input[name="picked"]')) : []);

    const refresh = () => {
      let checked = 0;
      boxes().forEach((box) => {
        const option = box.closest("[data-friend-option]");
        if (option) option.classList.toggle("selected", box.checked);
        if (box.checked) checked += 1;
      });
      if (countEl) countEl.textContent = String(checked);
    };

    if (list) list.addEventListener("change", refresh);
    if (search && list) {
      search.addEventListener("input", () => {
        const needle = search.value.trim().toLowerCase();
        Array.from(list.querySelectorAll("[data-friend-option]")).forEach((option) => {
          const haystack = (option.getAttribute("data-friend-name") || "").toLowerCase();
          option.hidden = Boolean(needle) && !haystack.includes(needle);
        });
      });
    }
    refresh();
  };

  roots.forEach(wirePicker);

  const statusEl = document.querySelector("[data-friend-status]");
  if (!statusEl) return;

  const stateText = statusEl.querySelector("[data-friend-state-text]");
  const statusText = statusEl.querySelector("[data-friend-status-text]");
  // 没读过、也没有已读结果时整条藏起来：状态条只在"正在读 / 读完了 / 读失败"时有意义
  const statusWrap = document.querySelector("[data-friend-status-wrap]");
  const ACTIVE = ["starting", "opening", "ready", "scanning"];
  const INTERVAL = 2000;
  let lastState = statusEl.dataset.friendState || "";
  let sawActive = ACTIVE.includes(lastState);

  const apply = (data) => {
    const session = (data && data.session) || {};
    const state = session.state || "";
    statusEl.dataset.friendState = state;
    if (statusWrap) {
      // idle 且什么都没读到 = 没什么好说的，整条藏起来
      const idle = state === "idle" || !state;
      statusWrap.hidden = idle && !session.active && !session.count && !session.error;
    }
    if (stateText) stateText.textContent = session.state_label || "";
    if (statusText) {
      const bits = [];
      // 服务端的 message 本身就带条数（"已读到 23 个会话"），只在它没说的才补一条
      if (session.message) bits.push(session.message);
      else if (session.count) bits.push(`已读到 ${session.count} 个会话`);
      if (session.error) bits.push(session.error);
      statusText.textContent = bits.join(" · ");
    }
    const active = Boolean(session.active);
    sawActive = sawActive || active;
    if (sawActive && !active) {
      sawActive = false;
      window.location.reload();     // 让服务端把读到的会话渲染成勾选项
      return;
    }
    lastState = state;
  };

  const poll = async () => {
    if (document.visibilityState !== "visible") return;
    try {
      const response = await fetch("/api/ops/friends", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) return;
      apply(await response.json());
    } catch (error) {
      /* 网络抖一下无所谓，下一次轮询会补上 */
    }
  };

  poll();
  window.setInterval(poll, INTERVAL);
})();


// 侧栏的几个入口指向收进 <details> 里的段落（#login-panel / #config-panel）。
// 浏览器不会为了锚点去展开折叠段，于是"点了没反应"，这里手动展开再滚过去。
(() => {
  const reveal = () => {
    const hash = window.location.hash;
    if (!hash || hash.length < 2) return;
    let target = null;
    try {
      target = document.querySelector(hash);
    } catch (error) {
      return;                       // 非法选择器（比如从别处粘来的 hash）忽略
    }
    if (!target) return;

    const boxes = [];
    const outer = target.closest("details");
    if (outer) boxes.push(outer);
    if (typeof target.querySelectorAll === "function") {
      boxes.push(...target.querySelectorAll("details"));
    }

    let opened = false;
    boxes.forEach((box) => {
      if (!box.open) {
        box.open = true;
        opened = true;
      }
    });
    if (opened && typeof target.scrollIntoView === "function") {
      target.scrollIntoView({ block: "start" });
    }
  };

  reveal();
  window.addEventListener("hashchange", reveal);
})();
