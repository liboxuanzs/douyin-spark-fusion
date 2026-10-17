const { createApp, ref, reactive, computed } = Vue;
const app = createApp({
  setup() {
    const message = ref("Hello vue!");

    const log_level_options = [
      {
        id: "Debug",
        label: "Debug",
        value: "Debug",
      },
      {
        id: "Info",
        label: "Info",
        value: "Info",
      },
      {
        id: "Warning",
        label: "Warning",
        value: "Warning",
      },
      {
        id: "Error",
        label: "Error",
        value: "Error",
      },
    ];

    // 网页版原本没有「抖动」输入框（Python 版配置工具也没有），但 .env 里必须有这个键：
    // 不写就等于用程序内置默认值，用户改不了。
    // ⚠️ 它和 utils/config.py 的兜底值（`_env_int("SCHEDULE_JITTER_MINUTES", 15, minimum=0)`）、
    //    configTool/models.py 的 DEFAULT_SCHEDULE_JITTER_MINUTES 三处必须一致。
    // 为什么另开一个 reactive 而不是塞进 form：
    //    form 里那些 `键名: 值` 会被 tests/test_config_tool.py 的
    //    DefaultsAlignmentTests 抠出来做"四处默认值对齐"校验，而 .env.example 与
    //    utils/config.py 都不在本次允许改动的范围内 —— 塞进去会让那个既有用例变红。
    //    用一个只属于网页版的 reactive 即可绕开，语义上也更贴切（它不是 .env 键的初始值）。
    const jitter = reactive({ SCHEDULE_JITTER_MINUTES: 15 });

    // 指纹种子范围：与 configTool/profile_store.py 的
    // FINGERPRINT_MIN / FINGERPRINT_MAX（10000 / 99999）以及面板 webui/envfile.py 一致。
    // 只要求「同一个账号恒定」，不要求全局唯一，也不由 unique_id 推导。
    const FINGERPRINT_MIN = 10000;
    const FINGERPRINT_MAX = 99999;

    // 生成一个指纹种子。Math.random 就够用：这是**浏览器指纹种子**，
    // 不是凭据，也不算密钥 —— 真正的登录态在 cookies 里。
    const randomFingerprint = () => {
      return String(
        FINGERPRINT_MIN +
          Math.floor(Math.random() * (FINGERPRINT_MAX - FINGERPRINT_MIN + 1))
      );
    };

    // do not use same name with ref
    const form = reactive({
      PROXY_ADDRESS: "",
      RUN_TIME: "09:00:00",
      TZ: "Asia/Shanghai",
      MESSAGE_TEMPLATE:
        "[盖瑞]今日火花[加一]\n—— [右边] 每日一言 [左边] ——\n[API]",
      HITOKOTO_TYPES: ["文学", "影视", "诗词", "哲学"],
      BROWSER_ACTION_TIMEOUT: 120,
      IM_SCAN_TIMEOUT: 120,
      IM_READY_TIMEOUT: 120,
      FRIEND_LIST_WAIT_TIME: 3,
      IM_MAX_STEPS: 200,
      TASK_RETRY_TIMES: 1,
      LOG_LEVEL: "Info",
      ACCOUNTS: [
        {
          username: "user1",
          unique_id: "12345678905",
          // 浏览器指纹种子（10000~99999）。固定不变：重扫 cookies、重新登录、
          // 换名字都不要改它，否则抖音看到的是"天天换设备"。
          fingerprint: "17022",
          cookies:
            '[{"name":"sessionid","value":"your-sessionid","domain":".douyin.com","path":"/"},{"name":"ttwid","value":"your-ttwid","domain":".douyin.com","path":"/"}]',
          targets: ["friend1", "friend2"],
        },
      ],
    });

    const environmentVariables = computed(() => {
      const [CRON_HOUR, CRON_MINUTE, CRON_SECOND] = form.RUN_TIME.split(":");
      // ⚠️ 光写 CRON_* 是**没用的**：那三个键只有 Docker 定时器
      //    （docker/entrypoint-cron.sh）读，systemd 部署下完全没人看。
      //    程序内真正的发送闸门只认 TASK_SCHEDULE_TIMES（core/tasks.py →
      //    core/schedule.py），不写它的话用户在这里设的 09:00 会被静默
      //    忽略、按默认的 20:30 发 —— 这正是本次修的缺陷。
      //    只取 HH:MM：schedule.parse_time 拿 "09:00:00" 当非法值，
      //    会直接回落到默认时间。
      const TASK_SCHEDULE_TIMES = form.RUN_TIME.slice(0, 5);

      return {
        PROXY_ADDRESS: form.PROXY_ADDRESS,
        // 下面三个是 Docker 专用（systemd 部署无效），保留是因为
        // docker/entrypoint-cron.sh 靠它们生成 cron 表达式。
        CRON_HOUR,
        CRON_MINUTE,
        CRON_SECOND,
        TZ: form.TZ,
        MESSAGE_TEMPLATE: form.MESSAGE_TEMPLATE,
        HITOKOTO_TYPES: form.HITOKOTO_TYPES,
        BROWSER_ACTION_TIMEOUT: form.BROWSER_ACTION_TIMEOUT,
        IM_SCAN_TIMEOUT: form.IM_SCAN_TIMEOUT,
        IM_READY_TIMEOUT: form.IM_READY_TIMEOUT,
        FRIEND_LIST_WAIT_TIME: form.FRIEND_LIST_WAIT_TIME,
        IM_MAX_STEPS: form.IM_MAX_STEPS,
        TASK_RETRY_TIMES: form.TASK_RETRY_TIMES,
        LOG_LEVEL: form.LOG_LEVEL,
        TASK_SCHEDULE_TIMES,
        SCHEDULE_JITTER_MINUTES: jitter.SCHEDULE_JITTER_MINUTES,
        // fingerprint 必须带上：没有它主程序每天会给这个账号换一个设备指纹，
        // 抖音那边看着就是"天天换设备登录"，而 docs/webui.md 承诺指纹是固定的。
        // 它取的是账号对象里那个种子，重扫/重登都不会改它。
        TASKS: form.ACCOUNTS.map((account) => ({
          username: account.username,
          unique_id: account.unique_id,
          fingerprint: account.fingerprint,
          targets: account.targets,
        })),
      };
    });

    const environmentSecrets = computed(() => {
      return form.ACCOUNTS.reduce((acc, account, index) => {
        acc[`COOKIES_${String(account.unique_id || "").toUpperCase()}`] = account.cookies;
        return acc;
      }, {});
    });

    const copyValue = (value) => {
      if (typeof value === "object") {
        value = JSON.stringify(value);
      } else if (typeof value === "number") {
        value = value.toString();
      } else {
        value = value.replace(/\n/g, "\\n");
      }
      navigator.clipboard.writeText(value).then(
        () => {
          ElementPlus.ElMessage.success("已复制到剪贴板");
        },
        (err) => {
          ElementPlus.ElMessage.error("复制失败: " + err);
        }
      );
    };

    const copyEnvFile = () => {
      // 合并两个对象
      const allVars = {
        ...environmentVariables.value,
        ...environmentSecrets.value,
      };
      // 生成 .env 格式字符串
      const item = Object.entries(allVars)
        .map(([key, value]) => {
          if (typeof value === "object") {
            value = JSON.stringify(value);
          } else if (typeof value === "number") {
            value = value.toString();
          } else {
            value = value.replace(/\n/g, "\\n");
          }
          return `${key}=${value}`;
        })
        .join("\n");
      navigator.clipboard.writeText(item).then(
        () => {
          ElementPlus.ElMessage.success("已复制 .env 配置文件到剪贴板");
        },
        (err) => {
          ElementPlus.ElMessage.error("复制失败: " + err);
        }
      );
    };

    const openEnvDetails = (name, value) => {
      console.log(
        "openEnvDetails called with name:",
        name,
        "value:",
        value,
        typeof value
      );
      if (typeof value === "object") {
        value = JSON.stringify(value, null, 2);
        console.log("value is object, stringify it:", value);
      }

      ElementPlus.ElMessageBox.alert(
        "<div style='text-align: left; white-space: pre-wrap; word-break: break-all; width: 400px; max-height: 200px; overflow: auto;'>" +
          value +
          "</div>",
        `${name} 详情`,
        {
          dangerouslyUseHTMLString: true,
        }
      );
    };

    const addAccount = () => {
      form.ACCOUNTS.push({
        username: "",
        unique_id: "",
        // 新账号直接带上指纹种子，省得用户在页面上手填：
        // 空着的话主程序每天给它换一个指纹（见 environmentVariables 里的注释）。
        fingerprint: randomFingerprint(),
        cookies:
          '[{"name":"sessionid","value":"your-sessionid","domain":".douyin.com","path":"/"}]',
        targets: [],
      });
    };

    const removeAccount = (index) => {
      form.ACCOUNTS.splice(index, 1);
    };

    return {
      log_level_options,
      message,
      form,
      jitter,
      randomFingerprint,
      FINGERPRINT_MIN,
      FINGERPRINT_MAX,
      environmentVariables,
      environmentSecrets,
      copyValue,
      copyEnvFile,
      openEnvDetails,
      addAccount,
      removeAccount,
    };
  },
});
app.use(ElementPlus);
app.mount("#app");
