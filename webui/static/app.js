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
  // 皮肤：aurora（极光棱镜，默认）与 classic（扁平控制台）互斥加载，切换时只换 link 的 href，不刷新页面。
  // 极光背景层 .aurora-scene 由这里注入 —— 它纯装饰，所以既不进模板也不进 app.css。
  const root = document.documentElement;
  let link = document.getElementById("app-stylesheet");
  const storageKey = "sparkflow-skin";
  const preset = window.__DSF_STYLES || null;

  const skinNow = () => (root.dataset.skin === "aurora" ? "aurora" : "classic");

  // 与模板头脚本同一套口径：没挑过就是 aurora，挑过 classic 的才留在 classic。
  const storedSkin = () => {
    try {
      return localStorage.getItem(storageKey) === "classic" ? "classic" : "aurora";
    } catch {
      return "aurora";
    }
  };

  const hrefFor = (skin) => {
    const fallback = (link && link.getAttribute("href")) || "/static/app.css";
    const pair = preset || {
      classic: fallback.replace("app-aurora.css", "app.css"),
      aurora: fallback.replace("app.css", "app-aurora.css"),
    };
    return skin === "aurora" ? pair.aurora : pair.classic;
  };

  let scene = null;
  const setScene = (skin) => {
    if (skin !== "aurora") {
      if (scene) {
        scene.remove();
        scene = null;
      }
      return;
    }
    if (scene || !document.body) return;
    // 全仓禁止用字符串拼 DOM 节点（tests/test_webui.py 里那条静态扫描），
    // 所以这层装饰也老老实实 createElement 一棵树出来。
    scene = document.createElement("div");
    scene.className = "aurora-scene";
    scene.setAttribute("aria-hidden", "true");
    const grid = document.createElement("div");
    grid.className = "aurora-grid";
    scene.appendChild(grid);
    ["a", "b", "c"].forEach((name) => {
      const blob = document.createElement("span");
      blob.className = "aurora-blob aurora-blob--" + name;
      scene.appendChild(blob);
    });
    const vignette = document.createElement("div");
    vignette.className = "aurora-vignette";
    scene.appendChild(vignette);
    document.body.insertBefore(scene, document.body.firstChild);
  };

  // 双缓冲换样式表：新表先挂上去，等它 load 完再摘掉旧表，切换过程中不会出现没样式的空档。
  // 正在等 load 的那张候选样式表。连续快速切换（双击皮肤按钮）时，上一张候选
  // 必须作废：否则它晚一步 load 完会把 id 抢回去，页面最终生效的表与
  // dataset / localStorage 记下的选择不一致，而且要多点一次才恢复。
  let pending = null;

  const swapSheet = (href) => {
    const current = link;
    if (!current) {
      const fresh = document.createElement("link");
      fresh.rel = "stylesheet";
      fresh.id = "app-stylesheet";
      fresh.href = href;
      document.head.appendChild(fresh);
      link = fresh;
      return;
    }
    if (pending) {
      // 新的切换取代了还没落地的那张：先摘掉，它的 load 回调看到 pending 变了就收手。
      pending.remove();
      pending = null;
    }
    if (current.getAttribute("href") === href) return;
    const next = current.cloneNode(false);
    next.removeAttribute("id");
    next.setAttribute("href", href);
    pending = next;
    next.addEventListener("load", () => {
      if (pending !== next) return;      // 已被后来的切换取代，别抢 id
      pending = null;
      current.remove();
      next.id = "app-stylesheet";
      link = next;
    });
    next.addEventListener("error", () => {
      if (pending === next) pending = null;
      next.remove();
      // 换表失败：dataset 与 localStorage 在 applySkin 里已经写成新皮肤了，必须回滚，
      // 否则页面继续用旧表、开关却记着新皮肤（再点一次才"恢复"）。从还挂着的旧表
      // 反推上一个皮肤，比另外存一份状态更不容易漂。
      const previous = (current.getAttribute("href") || "").indexOf("app-aurora.css") >= 0
        ? "aurora" : "classic";
      root.dataset.skin = previous;
      setScene(previous);
      try {
        localStorage.setItem(storageKey, previous);
      } catch {
        // 回滚记不住也不影响页面已经用回旧表。
      }
    });
    document.head.appendChild(next);
  };

  const applySkin = (skin, persist) => {
    const value = skin === "aurora" ? "aurora" : "classic";
    root.dataset.skin = value;
    swapSheet(hrefFor(value));
    setScene(value);
    if (persist) {
      try {
        localStorage.setItem(storageKey, value);
      } catch {
        // 记不住也不影响本次切换。
      }
    }
  };

  applySkin(root.dataset.skin || storedSkin(), false);

  document.querySelectorAll("[data-skin-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      applySkin(skinNow() === "aurora" ? "classic" : "aurora", true);
    });
  });

  // 背景视差 + 卡片上的鼠标跟随柔光：只在极光皮肤下算，且一帧最多写一次样式。
  // 三层光斑的视差系数：a 反向最大、b 同向、c 反向最小，拉开前后景。
  // 背景层是随皮肤注入/移除的（见上面的 setScene），所以引用要现查、按节点缓存，
  // 不能在这里抓一次就完 —— 切回极光皮肤时会是一个新节点。
  const PARALLAX_LAYERS = [
    [".aurora-blob--a", -26, -18],
    [".aurora-blob--b", 20, 14],
    [".aurora-blob--c", 14, -10],
  ];
  let parallaxHost = null;
  let parallax = [];
  const currentParallax = () => {
    const host = document.querySelector(".aurora-scene");
    if (!host || host === parallaxHost) return parallax;
    parallaxHost = host;
    parallax = PARALLAX_LAYERS.map(([selector, x, y]) => {
      const el = host.querySelector(selector);
      return el ? { el, x, y } : null;
    }).filter(Boolean);
    return parallax;
  };
  let pointer = null;
  let frame = 0;
  const paintGlow = () => {
    frame = 0;
    const event = pointer;
    pointer = null;
    if (!event || skinNow() !== "aurora") return;
    // 背景：指针位置归一化成 -0.5 ~ 0.5，按每层系数折成 px 位移，形成纵深。
    const layers = currentParallax();
    if (layers.length && window.innerWidth && window.innerHeight) {
      const px = event.clientX / window.innerWidth - 0.5;
      const py = event.clientY / window.innerHeight - 0.5;
      layers.forEach(({ el, x, y }) => {
        el.style.setProperty("--tx", (px * x).toFixed(1) + "px");
        el.style.setProperty("--ty", (py * y).toFixed(1) + "px");
      });
    }
    const card =
      event.target && event.target.closest
        ? event.target.closest(".metric-card, .overview-strip, .account-card")
        : null;
    if (!card) return;
    const rect = card.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    card.style.setProperty("--mx", Math.round(((event.clientX - rect.left) / rect.width) * 100) + "%");
    card.style.setProperty("--my", Math.round(((event.clientY - rect.top) / rect.height) * 100) + "%");
  };
  document.addEventListener(
    "pointermove",
    (event) => {
      if (event.pointerType === "touch" || skinNow() !== "aurora") return;
      pointer = event;
      if (!frame) frame = window.requestAnimationFrame(paintGlow);
    },
    { passive: true }
  );
  // 指针离开窗口时把视差归零，否则光斑会永远偏在一侧。
  document.addEventListener("pointerleave", () => {
    if (!parallax.length || skinNow() !== "aurora") return;
    parallax.forEach(({ el }) => {
      el.style.setProperty("--tx", "0px");
      el.style.setProperty("--ty", "0px");
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
  // 跨页转场兜底：不再用 CSS.supports / CSSStyleSheet 猜浏览器支不支持——
  // Firefox 157 和部分 Chromium 分支会"解析得出 @view-transition、实际不播"。
  // 做法是每次同源导航都写标记，新页 base.html 先挂上入场类；真原生转场发生时
  // pagereveal 带 viewTransition 在首帧前把类摘掉，没发生就留下类播 280ms 入场。
  // 只做新页入场、不做旧页离场：旧页先淡出会空出一截背景。
  const root = document.documentElement;
  const KEY = "sparkflow-nav";
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");

  // 入场类由 base.html 的同步脚本挂上，这里只负责在动画结束后摘掉。
  const finishEnter = () => root.classList.remove("dsf-page-enter");
  if (root.classList.contains("dsf-page-enter")) {
    const shell = document.querySelector(".app-shell");
    if (shell) {
      shell.addEventListener("animationend", (event) => {
        if (event.animationName === "dsf-enter-in") finishEnter();
      });
    }
    window.setTimeout(finishEnter, 900);
  }

  // 从 bfcache 回来时清掉入场类，避免它一直挂着。
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) root.classList.remove("dsf-page-enter");
  });

  if (reduce.matches) return;

  let leaving = false;
  const shouldSkip = (link, event) => {
    if (event.defaultPrevented || event.button !== 0) return true;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return true;
    if (!link || (link.target && link.target !== "_self")) return true;
    if (link.hasAttribute("download")) return true;
    if (link.closest("[data-confirm]")) return true;
    const href = link.getAttribute("href");
    if (!href || href.startsWith("#") || href.startsWith("javascript:") || href.startsWith("mailto:")) return true;
    let url;
    try {
      url = new URL(href, location.href);
    } catch (error) {
      return true;
    }
    if (url.origin !== location.origin) return true;
    // 同一页面只换 hash 是页内跳转，不该整页淡出。
    if (url.pathname === location.pathname && url.search === location.search && url.hash) return true;
    if (url.href === location.href) return true;
    return false;
  };

  document.addEventListener("click", (event) => {
    if (leaving) return;
    const link = event.target && event.target.closest ? event.target.closest("a[href]") : null;
    if (!link || shouldSkip(link, event)) return;
    const url = new URL(link.href, location.href);
    event.preventDefault();
    try {
      sessionStorage.setItem(KEY, JSON.stringify({ href: url.href, at: Date.now() }));
    } catch (error) {
      // 存储不可用：仍然直接导航，只是新页没法接入场。
    }
    leaving = true;
    location.assign(url.href);
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
  // 模板上挂的是 `data-segment-owner`（dashboard 的「运行模式」、runs 的「目标模式」）。
  // 这里原来只找 `data-segment-group` —— 全仓没有这个属性，于是整段是死代码：
  // 点了不切 .active、不切 aria-selected、不切面板的 hidden，`#dry-run-value`
  // 永远停在服务端默认档（2026-10-17 前端审计）。两个选择器都认，兼容旧写法。
  document
    .querySelectorAll("[data-segment-group], [data-segment-owner]")
    .forEach((group) => {
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

  // 顶部状态横幅：服务端 _banners() 是唯一的文案来源，这里只按它重画。
  // 为什么必须重画：冷却、禁跑窗、运行中、运行锁、账号阻断、上一轮跳过**全都会自己开始**
  // —— 时钟走进禁跑窗、上一轮命中风控、定时器每 5 分钟敲一次门 —— 只在首屏渲染一遍，
  // 结果就是"盯着页面反而看不见它开始"，那枚倒计时也永远等不到它的横幅
  // （2026-10-18 issue #10）。
  //
  // 只在内容变了才重建 DOM：每 10 秒重插一次会把倒计时的节点换掉（数字闪一下），
  // 而且白白丢掉浏览器的滚动/焦点状态。
  // 签名里**不含** untilText：它是每秒重算的倒计时文案（"还剩 9 分 58 秒"），
  // 每 10 秒都变一次；带上它等于每次轮询都重建 DOM（数字闪一下），
  // 而倒计时本来就有自己的每秒 tick（按 until 算），不需要靠重建来刷新。
  let bannerSignature = null;
  const bannerKey = (banner) =>
    [
      banner.kind,
      banner.level,
      banner.title,
      banner.text,
      banner.icon,
      banner.until,
    ].join("\u0000");
  const updateBanners = (banners) => {
    const list = document.querySelector("[data-status-banner-list]");
    if (!list) return;
    const signature = banners.map(bannerKey).join("\u0001");
    if (signature === bannerSignature) return;
    bannerSignature = signature;
    const tpl = document.querySelector("template[data-status-banner-tpl]");
    list.textContent = "";
    banners.forEach((banner) => {
      const node = tpl ? tpl.content.firstElementChild.cloneNode(true) : null;
      if (!node) return;
      node.className = `status-banner ${banner.level || "info"}`;
      const icon = node.querySelector("[data-status-banner-icon]");
      if (icon) icon.setAttribute("data-lucide", banner.icon || "info");
      const text = node.querySelector("[data-status-banner-text]");
      if (text) {
        // 与模板同一套拼法：有标题才露那个全角冒号（少一边，首屏与轮询就差一个字）。
        text.textContent = banner.title
          ? `${banner.title}：${banner.text || ""}`
          : banner.text || "";
      }
      const pill = node.querySelector("[data-countdown-until]");
      if (pill) {
        const until = banner.until || "";
        pill.hidden = !until;
        pill.dataset.countdownUntil = until;
        pill.className = `pill ${banner.level === "danger" ? "danger" : "info"}`;
        const countdown = pill.querySelector("[data-countdown-text]");
        if (countdown) countdown.textContent = banner.untilText || "";
      }
      list.appendChild(node);
    });
    renderIcons(); // 克隆来的 <i data-lucide> 也得点亮，不然横幅前面少个图标
  };

  // 服务端 viewmodel.STATE_CHIP 会发过来的四档配色类名（模板把它写进
  // `class="status-chip {{ account.chip }}"`）。放在这里当一份可被测试对着看的词表：
  // 服务端加了第五种配色、这里没跟上，就会出现"配色翻不过来"。
  const CHIP_NAMES = ["success", "warning", "danger", "info"];

  const updateAccounts = (accounts) => {
    accounts.forEach((account) => {
      const selector = `[data-account-overview="${CSS.escape(account.uniqueId)}"]`;
      document.querySelectorAll(selector).forEach((row) => {
        // 配色按 stateCss 来（"orphan" 这类状态没有自己的配色规则，服务端也是这么渲染的）
        row.dataset.accountState = account.stateCss || account.state;
        // 状态那枚 chip 的文字与配色一起换。只换边框颜色的话，账号从"已发送"翻成
        // "已跳过/失败"时会出现红边卡上写着"已发送"—— 最容易骗人的一种。
        row.querySelectorAll("[data-account-state-text]").forEach((node) => {
          if (account.stateLabel) {
            node.textContent = account.stateLabel;
          }
          if (account.chip) {
            // 只留服务端刚发的那个类，别的配色规则照旧留给 .status-chip 默认值
            CHIP_NAMES.forEach((name) => {
              node.classList.toggle(name, name === account.chip);
            });
          }
        });
        // "这一轮没有它的记录"：这一块必须真的不在页面上（不是 hidden —— 页面上有它、
        // 只是藏着，读源码/读屏都算"说了这句话"），所以只有它能被轮询显式增删。母版就是
        // 模板里同一块（<template data-account-run-missing-tpl>），克隆过来用；在 JS 里
        // 另写一遍文案，首屏和 10 秒后的轮询迟早各说各话。
        let missing = row.querySelector("[data-account-run-missing]");
        if (!account.runMissing) {
          if (missing) {
            missing.remove();
          }
        } else {
          if (!missing) {
            const tpl = document.querySelector("template[data-account-run-missing-tpl]");
            if (tpl) {
              missing = tpl.content.firstElementChild.cloneNode(true);
              // 位置也照首屏来：模板块就排在卡头与后面的提示/统计之间，插错地方
              // 等于同一句话在两次渲染里换了位置。
              const head = row.querySelector(".account-card-head");
              if (head) {
                head.after(missing);
              } else {
                row.appendChild(missing);
              }
              renderIcons(); // 克隆来的 <i data-lucide> 也得点亮，不然这一行少个图标
            }
          }
          if (missing) {
            missing.querySelectorAll("[data-account-run-missing-own]").forEach((own) => {
              own.hidden = !account.ownRunText;
            });
            missing
              .querySelectorAll("[data-account-run-missing-never]")
              .forEach((never) => {
                never.hidden = Boolean(account.ownRunText);
              });
            missing
              .querySelectorAll("[data-account-run-missing-own-text]")
              .forEach((text) => {
                text.textContent = account.ownRunText || "";
              });
          }
        }
        // 发送时间那一行：send_state 会随时间与轮次变（等 17:20 → 该发了 → 已发送），
        // 不跟着轮询重画就会出现"卡上写着已发送、时间那行还写着等 17:20 到点"。
        // 类名与文案按模板同一套拼法（time-state time-state-X + send_state_text）。
        row.querySelectorAll("[data-time-state]").forEach((node) => {
          const state = account.sendState || "";
          node.dataset.timeState = state;
          node.textContent = account.sendStateText || "";
          node.className = `time-state time-state-${state}`;
        });
        row.querySelectorAll("[data-time-window]").forEach((node) => {
          node.textContent = account.sendWindow || "";
        });
        // 内容摘要（"单独配了 N 行" / "用全局模板"）：与模板逐字一致。
        row.querySelectorAll("[data-message-summary]").forEach((node) => {
          node.textContent = account.hasMessage
            ? `这个账号单独配了内容（${account.messageLines || 0} 行），下一轮就发它`
            : "没单独配置，下一轮用全局模板";
        });
        // 出口（issue #18）：只写服务端给的遮掩文案（host:port），页面上永远不出现口令。
        // 自检结果那行**不**跟着轮询重画 —— 那是人点出来的结论，10 秒后自己消失等于没说。
        row.querySelectorAll("[data-proxy-text]").forEach((node) => {
          if (account.proxyText) {
            node.textContent = account.proxyText;
          }
        });
        row.querySelectorAll("[data-proxy-source]").forEach((node) => {
          if (account.proxySource) {
            node.textContent = account.proxySource;
          }
        });
        // 火花连续天数（issue #15）：断签每天都会变（昨天没发，今天这一行就该变红），
        // 不跟着轮询重画就会停在首屏那次的"连续 5 天"。文案全部来自服务端
        // （streakText 与 streakTargets[].text），前端不许自己拼一句 —— 两处各算一次，
        // 首屏和 10 秒后迟早各说各话。
        row.querySelectorAll("[data-streak-text]").forEach((node) => {
          if (account.streakText) {
            node.textContent = account.streakText;
          }
        });
        const streakList = row.querySelector("[data-streak-list]");
        if (streakList && Array.isArray(account.streakTargets)) {
          const items = account.streakTargets.map((item) => {
            const li = document.createElement("li");
            li.className =
              "streak-item" +
              (item.broken ? " is-broken" : item.known ? " is-ok" : " is-unknown");
            li.dataset.streakItem = item.key || "";
            const name = document.createElement("span");
            name.className = "streak-name";
            name.textContent = item.label || "";
            const days = document.createElement("span");
            days.className = "streak-days";
            days.dataset.streakDays = "";
            days.textContent = item.text || "";
            li.append(name, days);
            return li;
          });
          streakList.replaceChildren(...items);
        }
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
          // 与服务端模板保持同一句话：试运行必须标出来，不然轮询一刷就看不出来了。
          // "已送达 N" 不重复写 —— 左边那三个数字里已经有了。
          const dry =
            account.dryRun && account.dryRun !== "off"
              ? ` · 上一轮是试运行（${account.dryRunText || account.dryRun}）`
              : "";
          node.textContent = `已发送 ${strongCount + weakCount}/${accountTotal}${dry}`;
        });
      });
    });
  };

  // 运行期间不许动账号：重新登录会去抢同一份资料目录，移除会把正在跑的账号从 TASKS
  // 里删掉。挂点只有这一个 —— 原来的 updateActions() 还认 data-action-count-source /
  // data-action-count，而这两个属性在 templates/ 下命中数是 0，从 9c7a3a8 起就是死代码
  // （2026-10-18 issue #11），那半段已删掉。
  //
  // 只按 running 写 disabled 是不行的：服务端首屏还会因为"扫码会话开着"禁用重新登录，
  // 轮询手里没有那个状态，10 秒后就会把按钮重新点亮。所以服务端把这条理由也渲染成
  // data-login-active 发给这里，两处判断必须同源。
  // 第二个参数是**扫码会话是否还开着**（服务端 overview 的 login.active）。只按首屏
  // 那份 data-login-active 判断是不够的：会话被取消或结束后属性仍是 "1"，10 秒后
  // 轮询照样把按钮写回 disabled —— 用户看到的就是"重新登录永远是灰的、必须刷新页面"
  // （2026-10-20 的真实缺陷）。轮询拿到状态就同步回属性，两处判断才真正同源。
  const updateDisableWhileRunning = (running, loginActive) => {
    // 记住最近一次「任务是否在跑」：扫码那段轮询只带登录态过来、不带任务态，缺了它
    // 就会把「运行期间不许动账号」这条禁用理由弄丢（跑任务时按钮被点亮）。
    if (running !== undefined) updateDisableWhileRunning.lastRunning = Boolean(running);
    const runningNow =
      running === undefined
        ? updateDisableWhileRunning.lastRunning === true
        : Boolean(running);
    document.querySelectorAll("[data-disable-while-running]").forEach((button) => {
      if (loginActive !== undefined && button.hasAttribute("data-login-active")) {
        button.dataset.loginActive = loginActive ? "1" : "0";
      }
      button.disabled = runningNow || button.dataset.loginActive === "1";
    });
  };
  updateDisableWhileRunning.lastRunning = false;
  // 扫码面板那段轮询（2 秒一次）复用这一份判断：总览轮询 10 秒一次，取消扫码之后
  // 账号卡上的「重新登录」要灰满整整十秒，用户会以为按钮坏了（2026-10-30）。
  window.dsfSyncBusy = updateDisableWhileRunning;

  const render = (data) => {
    const summary = data.summary || {};
    const task = data.task || {};
    updateTaskBanner(task);
    updateBanners(data.banners || []);
    setText("[data-overview-value='total']", summary.total || 0);
    // 页头「· N 个账号」：目标数只算启用的账号，账号数若停在首屏那份旧值，
    // 停用/启用之后两边就各说各的。文案由服务端 accounts_text 一处给；
    // 老面板不带这个键时别去清空首屏已经渲染好的那半句。
    if (summary.accountsText) {
      setText("[data-overview-value='accounts']", summary.accountsText);
    }
    setText("[data-overview-value='confirmed']", summary.confirmed || 0);
    setText("[data-overview-value='pageEcho']", summary.pageEcho || 0);
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
    // 今日进度条：和服务端渲染的宽度算法一致（已送达橙段 + 可能已送达黄段）。
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
    // 这一格的说明必须跟着值一起换：值显示"-"却说一项核对通过的目标，等于自己打脸。
    // 措辞还必须与服务端首屏逐字一致，且描述真实判据：判定里收件人校验不过只是把证据
    // 降级成"可能已送达"（core/strict_verify.py 里 ok 仍为 True），说成"双确认"听起来比
    // 实际更严 —— 而这句话是用户判断"这条到底算不算发出去"的唯一依据。
    const lastConfirmedScope = summary.mixedRuns ? "各账号各自最近一次合计，" : "上一轮";
    setText(
      "[data-overview-help='lastConfirmedAt']",
      summary.lastConfirmedAt
        ? `${lastConfirmedScope}服务器确认已发出，并且确认发给了本人的目标`
        : summary.lastWeakAt
          ? `${lastConfirmedScope}只看到页面上的痕迹，没拿到服务器回执`
          : `${
              summary.mixedRuns ? "最近这些轮次里" : "上一轮"
            }没有确认成功的目标`,
    );
    // "各账号各自最近一次的结果（共 N 轮）"这块说明，服务端是按 mixed_runs 决定渲染与否的
    // （数字还可能停在首屏那一轮）。轮询只许切 hidden、不许增删 DOM，所以服务端必须发
    // 这个布尔值 —— 否则多账号各自跑过之后，轮询一刷这块说明就永远消失了。
    document.querySelectorAll("[data-overview-mixed-runs]").forEach((node) => {
      node.hidden = !summary.mixedRuns;
      node.querySelectorAll("[data-overview-mixed-runs-count]").forEach((count) => {
        count.textContent = summary.runCount || 0;
      });
    });
    // 试运行模式（"排练还是真发"）：药丸与两种说明也必须由轮询决定在不在页面上 ——
    // 模式会变（首页切档 → 立即运行），而首屏那一支是按"打开页面那一刻"的模式渲染的，
    // 轮询不重画，排练就被页面安安静静讲成了发送（2026-10-18 issue #9）。
    // 文案只写在模板里，这里只切 hidden 与那个模式文字槽。
    const dryRun = summary.dryRun || "off";
    const dryOn = dryRun !== "off";
    document.querySelectorAll("[data-overview-dry-run]").forEach((node) => {
      node.hidden = !dryOn;
      node.textContent = summary.dryRunText || "";
    });
    document.querySelectorAll("[data-overview-dry-run-hint]").forEach((node) => {
      // mixed：各账号模式不一致（每张卡自己写着）；single：整页只有一个模式。
      const mixed = node.dataset.overviewDryRunHint === "mixed";
      node.hidden = mixed ? dryRun !== "mixed" : !(dryOn && dryRun !== "mixed");
    });
    setText("[data-overview-dry-run-text]", summary.dryRunText || "");
    // 定时器没启用时，服务端渲染的是"不会自动跑"；轮询也必须说同一句话，
    // 不然页面每 2 秒就被前端改回一个骗人的时间点。
    const schedule = data.schedule || {};
    const armed = schedule.armed !== false;
    // 定时任务模式（各账号按自己的发送时间分开跑）：pending 讲的是"上一轮还剩几个
    // 目标没走到"，拿它当"下次触发还有活干"就会谎报 —— 那时候真话是
    // schedule.nextTriggerText（"下一个 示例号：今天 17:20" / "今天该发的都发完了"），
    // 服务端首屏写的也是它，轮询不能把它改回一个光秃秃的时间。
    const dueText = schedule.nextTriggerText || "";
    // 值槽那一行：定时任务模式下服务端给的是短句（"示例号 · 已过 17:20"），这里必须
    // 用同一套回落顺序和同样两个类名，否则轮询一刷字号/句式就跟首屏不一样了。
    const dueShort = schedule.nextTriggerShort || "";
    const dueValue = armed
      ? dueShort ||
        (schedule.dispatcher && dueText ? dueText : formatTime(schedule.nextTriggerAt)) ||
        "不会自动跑"
      : "不会自动跑";
    document
      .querySelectorAll("[data-overview-value='nextTriggerAt']")
      .forEach((node) => {
        node.textContent = dueValue;
        node.classList.toggle("overview-value--short", Boolean(armed && dueShort));
        node.classList.toggle(
          "overview-value--prose",
          Boolean(armed && !dueShort && schedule.dispatcher),
        );
      });
    setText(
      "[data-overview-value='scheduleLabel']",
      schedule.label || "-",
    );
    // 判断顺序必须与服务端首屏**逐字同序**：先"定时器启用着但 systemd 里已经挂了"
    // 的完整诊断，再"定时器没启用"，再定时任务那几种情况。定时器真挂了的时候首屏给的是
    // 坏在哪，轮询要是跳过了第一个分支，10 秒后同样的位置就只剩一句废话。
    setText(
      "[data-overview-value='nextTriggerHint']",
      schedule.timerStallText
        ? schedule.timerStallText
        : !armed
          ? "只能手动点「立即运行」"
          : schedule.dispatcher
            ? dueShort
              ? "到点后 5 分钟内会自动发出"
              : dueText
                ? "按各账号自己的时间发"
                : "今天该发的都发完了"
            // 末支必须与首屏同源：模板那一支写的就是 `timerText`（"定时器已启用"）。
            // 这里以前自己编了一句"仍有 N 个待发送目标" —— 那个数字在
            // `data-overview-value="pending"` 那格里已经有一份，10 秒后却把这句话
            // 换掉，正是 issue #2 说的"首屏与轮询互相打脸"。
            : schedule.timerText || "",
    );
    updateAccounts(data.accounts || []);
    const login = data.login || {};
    updateDisableWhileRunning(
      Boolean(task.running),
      login.active === undefined ? undefined : Boolean(login.active),
    );

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

  // 轮询：同一时刻只允许一个请求在飞（后端在读运行记录时单次可能超过 10 秒，
  // 旧写法会让下一个 tick 再发一个，旧响应后到还会覆盖新响应）；失败按
  // 10→20→40 秒退避（成功复位）；401 要说"登录已过期"，不能只说"更新延迟"
  // —— 会话过期后整页数字会一直停在旧值，用户却以为只是网络慢（2026-10-17 前端审计）。
  let inflight = false;
  let failures = 0;
  let timer = null;
  const markStale = (text) => {
    document.querySelectorAll("[data-overview-live-state]").forEach((node) => {
      node.textContent = text;
      node.classList.add("poll-stale");
    });
  };
  const schedule = () => {
    if (timer) window.clearTimeout(timer);
    const wait = Math.min(40000, 10000 * 2 ** Math.max(0, failures - 1));
    timer = window.setTimeout(refresh, wait);
  };
  const refresh = async () => {
    if (document.visibilityState !== "visible" || inflight) return;
    inflight = true;
    try {
      const response = await fetch("/api/ops/overview", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      if (response.status === 401) {
        markStale("登录已过期，请重新登录");
        return;
      }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      render(await response.json());
      failures = 0;
    } catch {
      failures += 1;
      if (failures >= 3) {
        const now = new Date().toLocaleTimeString("zh-CN", { hour12: false });
        markStale(`更新失败 · 最后一次尝试 ${now}`);
      } else {
        markStale("更新延迟");
      }
    } finally {
      inflight = false;
      schedule();
    }
  };

  document.querySelectorAll("[data-refresh-page]").forEach((button) => {
    button.addEventListener("click", () => window.location.reload());
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refresh();
  });
  refresh();
  schedule();
  window.addEventListener("pagehide", () => {
    if (timer) window.clearTimeout(timer);
  });
})();

// ---------------------------------------------------------------- 融合补充
// 以下两段是 C（DouYinSparkFlow）独有的能力，B 的分段控件与轮询里没有对应实现：
//   1) dry-run 模式：B 的分段控件只切换面板显隐，这里把它同步进表单；
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
      // 真实发送是不可撤回的动作，用实心红的主按钮（button-send）；试运行用柔和色。
      // 注意：这里是按模式**重画**按钮类的唯一位置 —— 只改模板里的 class 会被这段覆盖，
      // 所以两处必须写同一个类名（tests/test_webui.py 有契约测试钉住）。
      const real = level === "off";
      submit.className = real ? "button button-send" : "button button-soft";
      const label = submit.querySelector("[data-run-now-label]");
      if (label) label.textContent = real ? "立即发送" : "开始试运行";
    }
  };
  group.addEventListener("click", () => window.setTimeout(apply, 0));
  apply();
})();

(() => {
  // 节点**每次**重新取：横幅是 10 秒轮询重画的（冷却 / 禁跑窗会自己开始），
  // 只在加载时取一次的话，新出现的那条横幅永远没有倒计时（2026-10-18 issue #10）。
  const tick = () => {
    const now = Date.now();
    document.querySelectorAll("[data-countdown-until]").forEach((node) => {
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
// 轮询过后才插进页面的那些 <i data-lucide>（账号卡上克隆来的提示行）也要再点一次，
// 所以提成一个函数，两处共用。
const renderIcons = () => {
  if (window.lucide) {
    window.lucide.createIcons({ attrs: { "aria-hidden": "true" } });
  }
};
window.addEventListener("DOMContentLoaded", renderIcons);

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
    // 双向：会话结束时服务端不再给 has_shot，走不到刷新那段 —— 只写成单向的话
    // shot.hidden 会保持上一轮的 false，旧二维码留在页面上，用户可能去扫一张已经
    // 失效的码（而且 src 不清，浏览器还会拿缓存把它当新图显示）。
    if (shot) {
      shot.hidden = !session.has_shot;
      if (session.has_shot) {
        const shotSeq = String(session.shot_seq || "0");
        if (shotSeq !== lastShotSeq) {
          lastShotSeq = shotSeq;
          shot.src = `/ops/login/shot.png?v=${shotSeq}`;
        }
      } else {
        shot.removeAttribute("src");
      }
    }
    const active = Boolean(session.active);
    if (startBtn) startBtn.disabled = active;
    if (cancelBtn) cancelBtn.disabled = !active;
    // 账号卡上的「重新登录」跟着扫码会话一起动：它和这块面板是同一件事，只等 10 秒
    // 一次的总览轮询，取消之后会灰满十秒（2026-10-30 的真现场）。
    if (typeof window.dsfSyncBusy === "function") window.dsfSyncBusy(undefined, active);
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

// 磁盘与资料目录：展开时才去盘点（profile_report 要递归走每个资料目录，并进首页
// 渲染会把每一次翻页都拖慢），四个按钮 = 清缓存/清孤儿目录 × 预演/执行。
// 目录名与账号名都来自配置，所以结果一律用 textContent 写进 DOM，绝不拼 HTML。
(() => {
  const panel = document.querySelector("[data-maintenance-panel]");
  if (!panel) return;

  const statusEl = panel.querySelector("[data-maintenance-status]");
  const tailEl = panel.querySelector("[data-maintenance-disk-tail]");
  const diskEl = panel.querySelector("[data-maintenance-disk]");
  const lockedEl = panel.querySelector("[data-maintenance-locked]");
  const lockedTextEl = panel.querySelector("[data-maintenance-locked-text]");
  const bodyEl = panel.querySelector("[data-maintenance-body]");
  const totalsEl = panel.querySelector("[data-maintenance-totals]");
  const rowsEl = panel.querySelector("[data-maintenance-rows]");
  const emptyEl = panel.querySelector("[data-maintenance-empty]");
  const resultEl = panel.querySelector("[data-maintenance-result]");
  const resultHeadEl = panel.querySelector("[data-maintenance-result-head]");
  const resultListEl = panel.querySelector("[data-maintenance-result-list]");

  const LABELS = { caches: "清缓存", orphans: "清孤儿目录" };
  let loading = false;

  const setText = (node, text) => {
    if (node) node.textContent = text == null ? "" : String(text);
  };

  const showStatus = (text) => {
    if (!statusEl) return;
    setText(statusEl, text || "");
    statusEl.hidden = !text;
  };

  const showResult = (head, lines) => {
    if (!resultEl) return;
    setText(resultHeadEl, head);
    if (resultListEl) {
      resultListEl.replaceChildren();
      (lines || []).forEach((line) => {
        const li = document.createElement("li");
        li.textContent = line;
        resultListEl.appendChild(li);
      });
    }
    resultEl.hidden = false;
  };

  const chip = (text, tone) => {
    const span = document.createElement("span");
    span.className = "status-chip " + (tone || "");
    span.textContent = text;
    return span;
  };

  const renderDisk = (disk) => {
    const info = disk || {};
    setText(tailEl, info.tail || "展开时盘点");
    if (!diskEl) return;
    diskEl.hidden = !info.text;
    diskEl.className = "notice-inline maintenance-" + (info.level || "unknown");
    setText(diskEl, info.text || "");
  };

  const renderRows = (rows) => {
    if (!rowsEl) return;
    rowsEl.replaceChildren();
    (rows || []).forEach((item) => {
      const tr = document.createElement("tr");

      const nameCell = document.createElement("td");
      const name = document.createElement("strong");
      name.textContent = item.label || "";
      nameCell.appendChild(name);
      nameCell.appendChild(document.createElement("br"));
      const hint = document.createElement("small");
      hint.className = "maintenance-hint";
      hint.textContent = item.hint || "";
      nameCell.appendChild(hint);
      tr.appendChild(nameCell);

      [item.human_bytes, item.human_cache_bytes, item.idle_text].forEach((value) => {
        const td = document.createElement("td");
        td.textContent = value == null ? "" : String(value);
        tr.appendChild(td);
      });

      const tagCell = document.createElement("td");
      const tags = document.createElement("div");
      tags.className = "maintenance-tags";
      (item.tags || []).forEach((tag) => {
        // 在跑最要紧（清理会跳过它），其次才是孤儿、在册。
        const tone = item.in_use ? "warning" : item.is_orphan ? "danger" : "success";
        tags.appendChild(chip(tag, tone));
      });
      tagCell.appendChild(tags);
      tr.appendChild(tagCell);

      rowsEl.appendChild(tr);
    });
    if (emptyEl) emptyEl.hidden = (rows || []).length > 0;
  };

  const render = (data) => {
    const payload = data || {};
    const ok = Boolean(payload.ok);
    showStatus(payload.notice || "");
    renderDisk(payload.disk);
    if (lockedEl) lockedEl.hidden = ok;
    setText(lockedTextEl, payload.error || "");
    if (bodyEl) bodyEl.hidden = !ok;
    if (!ok) {
      // 名单读不到时「孤儿」判断没有意义（在册集合是空的），整张表都不显示，
      // 只留错误说明与磁盘那一行。执行按钮也随 body 一起藏起来。
      if (rowsEl) rowsEl.replaceChildren();
      return;
    }
    setText(totalsEl, (payload.totals || {}).text || "");
    renderRows(payload.rows);
  };

  const load = async () => {
    if (loading) return;
    loading = true;
    showStatus("正在盘点资料目录…");
    try {
      const response = await fetch("/api/ops/maintenance", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data || typeof data.ok !== "boolean") {
        showStatus((data && data.error) || "盘点失败：面板没能读到资料目录，稍后再展开一次");
        if (bodyEl) bodyEl.hidden = true;
        return;
      }
      render(data);
    } catch (error) {
      showStatus("盘点失败：请求没送到，稍后再展开一次");
      if (bodyEl) bodyEl.hidden = true;
    } finally {
      loading = false;
    }
  };

  const describeResult = (payload, what) => {
    if (!payload.ok) {
      showResult(`没有动任何文件：${what}失败`, [payload.error || "执行失败"]);
      return;
    }
    const removed = payload.removed || [];
    const lines = [`释放 ${payload.human_freed || "0 B"}（${removed.length} 项）`];
    removed.slice(0, 20).forEach((item) => {
      lines.push(`${item.label || item.name || ""}　${item.human_bytes || ""}`);
    });
    if (removed.length > 20) lines.push(`……还有 ${removed.length - 20} 项没列出来`);
    [
      ["正在跑（清了会打断发送）", payload.skipped_in_use],
      [`还在 ${payload.grace_days} 天宽限期内`, payload.skipped_recent],
      ["符号链接（不跟）", payload.skipped_symlink],
    ].forEach(([reason, names]) => {
      if (names && names.length) {
        lines.push(`跳过 ${names.length} 个 —— ${reason}：${names.join("、")}`);
      }
    });
    lines.push(
      payload.applied
        ? "已落盘。空闲时再展开一次，上面的数字会跟着变。"
        : "这只是预演：一个文件都没删。要真删就点同一行的「执行」。"
    );
    showResult(payload.headline || `${what}完成`, lines);
  };

  const run = async (button) => {
    const action = button.getAttribute("data-maintenance-action") || "";
    const apply = button.getAttribute("data-apply") === "1";
    const what = LABELS[action] || action;
    if (apply) {
      // 真删不可逆：确认文案里写清删什么、不删什么，别让人靠猜。
      const agreed = window.confirm(
        `确定要真的${what}吗？\n\n` +
          "这是真删（不是预演）：只删可再生的缓存 / 已不在账号名单里的资料目录，\n" +
          "在册账号、Cookies、Local Storage、Local State 都不会动。\n\n" +
          "建议先点「预演」看一眼将删掉什么、能释放多少。"
      );
      if (!agreed) return;
    }
    button.disabled = true;
    showResult(`正在${apply ? "执行" : "预演"}：${what}…`, ["预演只读不写；执行按上面的白名单真删。"]);
    try {
      const response = await fetch(`/ops/maintenance/${action}`, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          Accept: "application/json",
        },
        body: new URLSearchParams({ apply: apply ? "1" : "0" }),
      });
      const data = await response.json().catch(() => ({}));
      describeResult(data || {}, what);
      // 数字已经变了，顺手重盘一次（只刷新这一块，不做任何自动清理）。
      load();
    } catch (error) {
      showResult("请求没送到：什么都没删", ["网络断了，重新展开这一块再试一次。"]);
    } finally {
      button.disabled = false;
    }
  };

  // 每次展开都重盘：这里是手动工具，不需要后台轮询。
  panel.addEventListener("toggle", () => {
    if (panel.open) load();
  });
  if (panel.open) load();

  panel.querySelectorAll("[data-maintenance-action]").forEach((button) => {
    button.addEventListener("click", () => run(button));
  });
})();

// == 23 精修层的运行时（样式见 app-aurora.css 分区 23）=====================
// 这段只做三件「需要一个时钟或一次事件」的事：指挥条滚动投影、进度条首屏生长、
// 主题切换的圆形扩散。任何一件失败都不影响功能，因此每段都各自兜底。
(() => {
  const root = document.documentElement;
  const reduceMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // -- 23.1 指挥条：滚过一点之后才落下投影，静止时保持「贴在画布上」 --------
  const bar = document.querySelector(".sidebar");
  if (bar) {
    let queued = false;
    const sync = () => {
      queued = false;
      bar.classList.toggle("is-scrolled", window.scrollY > 8);
    };
    window.addEventListener("scroll", () => {
      if (queued) return;
      queued = true;
      window.requestAnimationFrame(sync);
    }, { passive: true });
    sync();
  }

  // -- 23.2 今日进度条：首屏从 0 长到目标值，只做一次 -----------------------
  // 之后 10 秒轮询改的是 width，值没变就不会重放；用户能看见的「在动」只有这一次。
  if (!reduceMotion()) {
    window.requestAnimationFrame(() => {
      const barStrong = document.querySelector("[data-overview-bar-strong]");
      const target = barStrong && barStrong.style.width;
      if (!barStrong || !target || target === "0%") return;
      barStrong.style.transition = "none";
      barStrong.style.width = "0%";
      // 读一次布局，把 0% 固化成过渡起点，否则同一帧里改回去不会有动画。
      void barStrong.offsetWidth;
      barStrong.style.transition = "";
      barStrong.style.width = target;
    });
  }

  // -- 23.3 主题切换：以按钮为圆心扩散（View Transitions 不可用就走原逻辑） -
  const paintTheme = (theme) => {
    root.dataset.theme = theme;
    root.style.colorScheme = theme;
    try {
      localStorage.setItem("sparkflow-theme", theme);
    } catch (error) {
      // 存储不可用时当前页照样切换，和 app.js 顶部那段的约定一致。
    }
  };

  const switchTheme = (button) => {
    const next = root.dataset.theme === "light" ? "dark" : "light";
    if (reduceMotion() || typeof document.startViewTransition !== "function") {
      paintTheme(next);
      return;
    }
    const rect = button.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;
    const radius = Math.hypot(
      Math.max(x, window.innerWidth - x),
      Math.max(y, window.innerHeight - y)
    );
    root.style.setProperty("--vt-x", Math.round(x) + "px");
    root.style.setProperty("--vt-y", Math.round(y) + "px");
    root.style.setProperty("--vt-r", Math.ceil(radius) + "px");
    root.classList.add("dsf-theme-vt");
    const transition = document.startViewTransition(() => paintTheme(next));
    transition.finished.finally(() => root.classList.remove("dsf-theme-vt"));
  };

  // 捕获阶段先于顶部的主题 IIFE 拿到点击：扩散曲面的圆心必须是按钮本身，
  // 而 View Transition 的回调要等旧画面拍完才跑 —— 交给原本的冒泡处理器就来不及了。
  document.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target.closest("[data-theme-toggle]") : null;
    if (!target) return;
    if (reduceMotion() || typeof document.startViewTransition !== "function") return;
    event.preventDefault();
    event.stopPropagation();
    switchTheme(target);
  }, true);
})();

// == 动效精修（2026-10-24）＝ app-aurora.css 第 24 分区 =====================
// 这一层只动「怎么动」。折叠区块的高度过渡已经在 CSS 里，这里补三件 CSS 做不到
// 的事：分段控件的滑块、数字增长、首屏以外区块的滚动揭示。
// 整段先判 reduced-motion —— 系统要求减少动效时一件事都不做，页面回到服务端
// 直出的样子（滑块退化成原来的背景高亮，数字直接是终值，区块全部可见）。
(() => {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

  // -- 24.3 分段控件的滑块 ------------------------------------------------
  // 只负责「量」和「摆」：谁该亮仍然由原来的两段脚本决定（切 .active）。
  // JS 没跑起来时 .has-thumb 不会出现，样式退回原来那套，不会两边都没高亮。
  const initSegmentedThumb = () => {
    const groups = [...document.querySelectorAll(".segmented-control")];
    if (!groups.length) return;

    const place = (group, animate) => {
      const active = group.querySelector(".segment-button.active");
      if (!active) return;
      let thumb = group.querySelector(".segment-thumb");
      if (!thumb) {
        thumb = document.createElement("span");
        thumb.className = "segment-thumb";
        thumb.setAttribute("aria-hidden", "true");
        // 关键三条写成内联：经典皮肤没有 .segment-thumb 的样式，宠物要是留在
        // flex 流里就会多出一个 3px 的 gap，把按钮挤歪。绝对定位＋透明＝无副作用。
        thumb.style.position = "absolute";
        thumb.style.left = "0";
        thumb.style.top = "0";
        thumb.style.pointerEvents = "none";
        group.prepend(thumb);
      }
      const box = group.getBoundingClientRect();
      const rect = active.getBoundingClientRect();
      // 还没显示的挂点（hidden 面板里）量出来是 0×0；等它出现时 ResizeObserver 会补一次
      if (!rect.width || !rect.height) return;
      if (!animate) group.classList.add("no-thumb-anim");
      group.classList.add("has-thumb");
      thumb.style.width = Math.round(rect.width) + "px";
      thumb.style.height = Math.round(rect.height) + "px";
      // 绝对定位的原点是父级的 padding box，rect 差里还含着父级 1px 边框
      const x = Math.round(rect.left - box.left - group.clientLeft);
      const y = Math.round(rect.top - box.top - group.clientTop);
      thumb.style.transform = "translate3d(" + x + "px, " + y + "px, 0)";
      if (!animate) {
        // 读一次 offsetWidth 把「无过渡落位」这一帧定死，再恢复过渡；
        // 否则同一帧里恢复的 transition 会把这次落位也当成一次位移播出来。
        void thumb.offsetWidth;
        window.requestAnimationFrame(() => group.classList.remove("no-thumb-anim"));
      }
    };

    const placeAll = (animate) => groups.forEach((group) => place(group, animate));

    placeAll(false);
    window.addEventListener("resize", () => placeAll(false), { passive: true });
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(() => placeAll(false)).catch(() => {});
    }
    // 切 .active 的是另外两段脚本（分段控件、试运行模式同步），一个在冒泡里直接切、
    // 一个用 setTimeout(…, 0)；这里一律等一拍再量，才量得到新的当前项。
    document.addEventListener("click", (event) => {
      const button = event.target instanceof Element ? event.target.closest(".segment-button") : null;
      if (!button) return;
      const group = button.closest(".segmented-control");
      if (group) window.setTimeout(() => place(group, true), 0);
    });
    // 面板从隐藏变显示、窄屏换行、字体替换都会让按钮第一次有尺寸或换位置
    if (typeof ResizeObserver === "function") {
      const observer = new ResizeObserver((entries) => {
        entries.forEach((entry) => {
          const group = entry.target;
          const width = Math.round(group.getBoundingClientRect().width);
          if (width === Number(group.dataset.thumbWidth || 0)) return;
          group.dataset.thumbWidth = String(width);
          place(group, false);
        });
      });
      groups.forEach((group) => observer.observe(group));
    }
  };
  initSegmentedThumb();

  // -- 24.4 数字增长 ------------------------------------------------------
  // 只处理「纯整数」和「百分数」：今日进度那一格是 5/5，没数据时后端还可能给
  // 破折号 —— 一律原样显示。把破折号数成 0 再数回破折号，比不动更糟。
  const initCounters = () => {
    const nodes = [...document.querySelectorAll("[data-overview-value]")];
    if (!nodes.length) return;
    const state = new WeakMap();

    const parse = (text) => {
      const match = /^(\d{1,7})(%?)$/.exec(String(text == null ? "" : text).trim());
      return match ? { value: Number(match[1]), suffix: match[2] } : null;
    };
    // 尽量只改那个文本节点：整节点 textContent 重写会把子元素（比如单位）一起删掉
    const write = (node, text) => {
      const only = node.childNodes.length === 1 ? node.firstChild : null;
      if (only && only.nodeType === 3) only.nodeValue = text;
      else node.textContent = text;
    };
    const animate = (node, from, to, suffix, duration) => {
      const record = state.get(node) || {};
      record.busy = true;
      state.set(node, record);
      const started = performance.now();
      const step = (now) => {
        const t = Math.min(1, (now - started) / duration);
        const eased = 1 - Math.pow(1 - t, 3);
        write(node, Math.round(from + (to - from) * eased) + suffix);
        if (t < 1) {
          window.requestAnimationFrame(step);
          return;
        }
        // 末帧写回目标值本身，避免四舍五入停在 99% 这种尴尬处
        write(node, to + suffix);
        record.value = to;
        record.busy = false;
      };
      window.requestAnimationFrame(step);
    };
    const handle = (node) => {
      const record = state.get(node) || {};
      // 自己写进去的每一帧都会再触发一次观察器，靠 busy 挡掉，否则递归到天荒地老
      if (record.busy) return;
      const current = parse(node.textContent);
      if (!current) {
        state.set(node, { value: null });
        return;
      }
      const previous = record.value;
      record.value = current.value;
      state.set(node, record);
      if (document.hidden) return;
      if (current.value === previous) return;
      // 首屏没有「上一个值」：从 0 长上来，让这一屏一进来就在动
      const from = previous == null ? 0 : previous;
      if (from === current.value) return;
      animate(node, from, current.value, current.suffix, previous == null ? 720 : 520);
    };

    nodes.forEach(handle);
    // 总览每 10 秒把这一格用 textContent 重写一次，只有观察器接得住
    const observer = new MutationObserver((records) => {
      const seen = new Set();
      records.forEach((record) => {
        const target = record.target.nodeType === 3 ? record.target.parentElement : record.target;
        if (!target || typeof target.closest !== "function") return;
        const node = target.closest("[data-overview-value]");
        if (!node || seen.has(node)) return;
        seen.add(node);
        handle(node);
      });
    });
    document.querySelectorAll("[data-overview-root]").forEach((root) => {
      observer.observe(root, { subtree: true, childList: true, characterData: true });
    });
  };
  initCounters();

  // -- 24.5 滚动揭示 ------------------------------------------------------
  // 首屏那几块在 640ms 内就演完了，折叠线以下的还要滚很久才看得到，等到那时
  // 动画早已结束 —— 等于白给。这里只挑视口外的，滚到了再演一次。
  const initScrollReveal = () => {
    if (!("IntersectionObserver" in window)) return;
    const sections = [...document.querySelectorAll(".dashboard-stack > .dashboard-section")];
    if (!sections.length) return;
    // 首屏已经看得见的不要碰，它们由 CSS 的 dsf-rise 直接演
    const waiting = sections.filter((section) => {
      const rect = section.getBoundingClientRect();
      return rect.top > window.innerHeight * 0.92;
    });
    if (!waiting.length) return;
    const show = (section) => {
      section.classList.remove("dsf-await");
      section.classList.add("dsf-reveal");
    };
    try {
      waiting.forEach((section) => section.classList.add("dsf-await"));
      const observer = new IntersectionObserver(
        (entries) => {
          entries.forEach((entry) => {
            if (!entry.isIntersecting) return;
            observer.unobserve(entry.target);
            show(entry.target);
          });
        },
        { rootMargin: "0px 0px -6% 0px", threshold: 0.01 },
      );
      waiting.forEach((section) => observer.observe(section));
      // 兜底：观察器不触发（长页面一次渲染、打印、别的脚本改了布局）时 2.5 秒后
      // 一律显示 —— 宁可少一次动画，不能少一块内容。
      window.setTimeout(() => waiting.forEach((section) => show(section)), 2500);
      window.addEventListener("pagehide", () => observer.disconnect(), { once: true });
    } catch (error) {
      waiting.forEach((section) => show(section));
    }
  };
  initScrollReveal();
})();

// issue #18：出口自检。只在人点按钮时联网一次 —— 发送链路里绝不额外发外网请求（那会
// 变成每轮都要付的时间与风险，还会把"发送失败"和"自检失败"混成一团）。
// 结果写回这张卡的 [data-proxy-check-text]，文案由服务端给（只有它知道"和上次不一样"）。
(() => {
  const run = async (button) => {
    const uid = button.dataset.proxyCheck || "";
    if (!uid) {
      return;
    }
    const card = button.closest("[data-proxy-editor]");
    const out = card ? card.querySelector("[data-proxy-check-text]") : null;
    const label = button.querySelector("span");
    const restore = label ? label.textContent : "";
    button.disabled = true;
    if (label) {
      label.textContent = "自检中…";
    }
    if (out) {
      out.textContent = "正在通过这个出口访问回显服务…";
      delete out.dataset.proxyCheckState;
    }
    try {
      const body = new URLSearchParams();
      body.set("unique_id", uid);
      const resp = await fetch("/ops/account/proxy-check", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: body.toString(),
        credentials: "same-origin",
      });
      const data = await resp.json().catch(() => null);
      if (out) {
        out.textContent =
          (data && (data.text || data.error)) || "自检没走通（响应不是预期的 JSON）";
        out.dataset.proxyCheckState = data && data.ok ? (data.changed ? "changed" : "ok") : "fail";
      }
    } catch (err) {
      if (out) {
        out.textContent = "自检没走通：" + (err && err.message ? err.message : String(err));
        out.dataset.proxyCheckState = "fail";
      }
    } finally {
      button.disabled = false;
      if (label) {
        label.textContent = restore || "自检";
      }
    }
  };

  // 按钮由服务端渲染（账号卡随首屏一起出来），用委托就不用管轮询重画。
  document.addEventListener("click", (event) => {
    const button = event.target.closest ? event.target.closest("[data-proxy-check]") : null;
    if (!button) {
      return;
    }
    event.preventDefault();
    run(button);
  });
})();
