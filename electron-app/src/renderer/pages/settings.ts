import type { AppContext, PageDefinition } from "../context";
import { pageHead } from "../context";
import { banner, button, card, el, field, numberInput, select, switchRow, textInput } from "../ui";

/**
 * Behaviour, message shaping, storage, and diagnostics.
 *
 * This page also owns the configuration the 1.x GUI never surfaced: the polling interval,
 * ignored keywords, human-like reply delay, and conversation history depth. Each of those
 * existed in the engine and in the example config but had no way to be edited.
 */

let keywordDraft = "";
let pendingPrefix: string | null = null;
let diagnostics: Record<string, unknown> | null = null;

const REPO_URL = "https://github.com/Luofeng-Cloud/WeChat-AI-AutoReply";

export const settingsPage: PageDefinition = {
  id: "settings",
  label: "设置与诊断",
  glyph: "🔧",
  group: "系统",
  dispose() {
    diagnostics = null;
  },
  render(ctx) {
    const config = ctx.config;
    if (!config) return el("div", { class: "page" }, [pageHead("设置与诊断", "正在加载…")]);

    const rows: Node[] = [];

    rows.push(el("div", { class: "row-inline" }, [
      switchRow("在回复前添加前缀", config.include_prefix, async (value) => {
        await ctx.invokeConfig({ include_prefix: value });
        ctx.rerender();
      }, "让好友知道你用了自动回复"),
      el("div", { style: "flex:1" }, [
        textInput(config.reply_prefix, {
          placeholder: "[AI自动回复] ",
          onInput: (value) => { pendingPrefix = value; },
        }),
      ]),
      button("保存前缀", async () => {
        if (pendingPrefix === null) return;
        await ctx.invokeConfig({ reply_prefix: pendingPrefix });
        pendingPrefix = null;
        ctx.toast("ok", "前缀已保存");
      }, "ghost", "btn--sm"),
    ]));

    rows.push(el("div", { class: "sep" }));

    rows.push(el("div", { class: "grid grid--3" }, [
      field("拟人回复延迟（秒）", numberInput(config.reply_delay_seconds, {
        min: 0, max: 120, step: 0.5,
        onChange: async (value) => {
          await ctx.invokeConfig({ reply_delay_seconds: value });
          ctx.toast("ok", "已保存回复延迟");
        },
      }), "收到消息后等待这么久再回复，模拟真人打字思考的节奏。"),
      field("随机抖动（秒）", numberInput(config.reply_delay_jitter_seconds ?? 0, {
        min: 0, max: 60, step: 0.5,
        onChange: async (value) => {
          await ctx.invokeConfig({ reply_delay_jitter_seconds: value });
          ctx.toast("ok", "已保存随机抖动");
        },
      }), "在延迟基础上追加 0 到该值的随机时长，让回复节奏更自然。"),
      field("对话记忆轮数", numberInput(config.max_history_turns ?? 8, {
        min: 1, max: 40, step: 1,
        onChange: async (value) => {
          await ctx.invokeConfig({ max_history_turns: value });
          ctx.toast("ok", "已保存记忆轮数");
        },
      }), "每次请求会带上最近这么多轮的往返对话，让回复更有上下文。"),
    ]));

    rows.push(el("div", { class: "grid grid--2" }, [
      field("巡检间隔（秒）", numberInput(config.check_interval_seconds, {
        min: 0.1, max: 60, step: 0.1,
        onChange: async (value) => {
          await ctx.invokeConfig({ check_interval_seconds: value });
          ctx.toast("ok", "已保存巡检间隔");
        },
      }), "守护进程两次扫描之间的间隔。0.5 到 1.0 秒是响应速度与 CPU 占用的平衡点。"),
      field("日志级别", select(config.log_level, [
        { value: "debug", label: "debug —— 最详细，排查问题用" },
        { value: "info", label: "info —— 默认" },
        { value: "warn", label: "warn —— 只记录异常" },
        { value: "error", label: "error —— 只记录失败" },
      ], async (value) => {
        await ctx.invokeConfig({ log_level: value });
        ctx.toast("ok", "日志级别已保存", "引擎重启后完全生效。");
      }), "排查识别问题时建议临时改为 debug。"),
    ]));

    rows.push(el("div", { class: "sep" }));
    rows.push(renderKeywordEditor(ctx, config.ignored_keywords ?? []));
    rows.push(el("div", { class: "sep" }));
    rows.push(renderConfigIo(ctx));

    return el("div", { class: "page" }, [
      pageHead("设置与诊断", "微调回复行为，导出备份，以及在出现问题时收集环境信息。"),
      card("行为与消息格式", "影响回复的时机与呈现方式", rows),
      renderDiagnosticsCard(ctx),
      renderAboutCard(ctx),
    ]);
  },
};

function renderKeywordEditor(ctx: AppContext, keywords: string[]): HTMLElement {
  const chips = keywords.length === 0
    ? [el("span", { class: "muted", text: "暂无自定义屏蔽词（内置屏蔽词始终生效）" })]
    : keywords.map((keyword) => el("span", { class: "tag", style: "gap:4px" }, [
        el("span", { text: keyword }),
        el("button", {
          class: "btn btn--sm btn--ghost",
          style: "padding:0 4px;border:0",
          text: "✕",
          title: "移除",
          onclick: async () => {
            await ctx.invokeConfig({ ignored_keywords: keywords.filter((item) => item !== keyword) });
            ctx.rerender();
          },
        }),
      ]));

  return el("div", { class: "field" }, [
    el("label", { text: `屏蔽关键词（${keywords.length} 个）：识别到这些内容时不当作好友消息` }),
    el("div", { class: "row-inline", style: "flex-wrap:wrap;gap:6px" }, chips),
    el("div", { class: "row-inline", style: "margin-top:8px" }, [
      el("div", { style: "flex:1" }, [
        textInput(keywordDraft, {
          placeholder: "例如：正在输入、对方正在讲话",
          onInput: (value) => { keywordDraft = value; },
        }),
      ]),
      button("添加屏蔽词", async () => {
        const value = keywordDraft.trim();
        if (!value) return;
        if (keywords.includes(value)) {
          ctx.toast("warn", "该关键词已存在");
          return;
        }
        await ctx.invokeConfig({ ignored_keywords: [...keywords, value] });
        keywordDraft = "";
        ctx.rerender();
      }),
      button("恢复默认", async () => {
        await ctx.invokeConfig({ ignored_keywords: ctx.presets?.ignoredKeywordsDefaults ?? [] });
        ctx.toast("ok", "已恢复默认屏蔽词");
        ctx.rerender();
      }, "ghost", "btn--sm"),
    ]),
    el("div", { class: "hint", text: "内置屏蔽词（按住说话、语音输入文字、发送(S) 等）始终生效，这里的自定义项会追加在其后。" }),
  ]);
}

function renderConfigIo(ctx: AppContext): HTMLElement {
  return el("div", {}, [
    el("div", { class: "row-inline" }, [
      button("导出配置备份", async () => {
        const masked = await ctx.invoke("config_masked");
        const result = await window.engine.exportConfig(masked);
        if ((result as { ok?: boolean }).ok) ctx.toast("ok", "已导出配置", String((result as { path?: string }).path));
        else if (!(result as { canceled?: boolean }).canceled) ctx.toast("error", "导出失败", String((result as { error?: string }).error ?? ""));
      }),
      button("导入配置", async () => {
        const picked = await window.engine.importConfig();
        if (!(picked as { ok?: boolean }).ok) {
          if (!(picked as { canceled?: boolean }).canceled) {
            ctx.toast("error", "导入失败", String((picked as { error?: string }).error ?? ""));
          }
          return;
        }
        const document = (picked as { document: Record<string, unknown> }).document;
        const confirmed = await ctx.confirm({
          title: "导入配置？",
          body: "当前的 API Key、白名单、人设与提示词都会被文件中的内容覆盖。建议先导出备份。",
          confirmLabel: "覆盖导入",
          danger: true,
        });
        if (!confirmed) return;
        await ctx.invokeConfig(document as never);
        await ctx.refresh(true);
        ctx.toast("ok", "配置已导入");
        ctx.rerender();
      }),
      button("从磁盘重新读取", async () => {
        ctx.config = await ctx.invoke("config_reload");
        ctx.toast("ok", "已重新读取配置文件");
        ctx.rerender();
      }, "ghost"),
    ]),
    el("div", { class: "hint", text: "导出的配置会自动隐藏 API Key，可安全分享或附在 issue 中。导入时会原样覆盖所有字段。" }),
  ]);
}

function renderDiagnosticsCard(ctx: AppContext): HTMLElement {
  const rows: Node[] = [];
  rows.push(el("div", { class: "row-inline" }, [
    button("运行环境自检", async () => {
      diagnostics = (await ctx.invoke("diagnostics")) as unknown as Record<string, unknown>;
      ctx.rerender();
    }, "primary"),
    button("复制诊断信息", async () => {
      const info = await ctx.invoke("diagnostics");
      try {
        await navigator.clipboard.writeText(JSON.stringify(info, null, 2));
        ctx.toast("ok", "诊断信息已复制", "提交 issue 时请附上这段内容。");
      } catch {
        ctx.toast("warn", "无法写入剪贴板");
      }
    }, "ghost"),
  ]));

  if (diagnostics) {
    const packages = (diagnostics.sitePackages ?? {}) as Record<string, string>;
    const missing = Object.entries(packages).filter(([, version]) => version.startsWith("missing"));
    if (missing.length > 0) {
      rows.push(banner("danger", "缺少运行依赖",
        `${missing.map(([name]) => name).join("、")} 未安装。请在项目目录执行：pip install -r requirements.txt`));
    }
    rows.push(el("pre", {
      class: "mono",
      style: "white-space:pre-wrap;word-break:break-word;background:var(--bg-2);border:1px solid var(--line);border-radius:7px;padding:10px 12px;margin:12px 0 0;font-size:11px;line-height:1.7;max-height:340px;overflow:auto",
      text: JSON.stringify(diagnostics, null, 2),
    }));
  } else {
    rows.push(el("div", { class: "hint", text: "自检会报告 Python 版本、OCR 模型状态、依赖包版本、配置文件路径与数据目录，便于快速定位问题。" }));
  }

  const appInfo = ctx.appInfo;
  if (appInfo) {
    rows.push(el("div", { class: "sep" }));
    rows.push(el("div", { class: "grid grid--3" }, [
      el("div", { class: "metric" }, [el("div", { class: "metric__label", text: "应用版本" }), el("div", { class: "metric__value", text: String(appInfo.version ?? "—") })]),
      el("div", { class: "metric" }, [el("div", { class: "metric__label", text: "Electron" }), el("div", { class: "metric__value", text: String(appInfo.electron ?? "—") })]),
      el("div", { class: "metric" }, [el("div", { class: "metric__label", text: "Node" }), el("div", { class: "metric__value", text: String(appInfo.node ?? "—") })]),
    ]));
    rows.push(el("div", { class: "hint", text: `数据目录：${String(appInfo.userData ?? "")}` }));
  }

  return card("诊断信息", "排查问题时请先运行一次自检", rows);
}

function renderAboutCard(ctx: AppContext): HTMLElement {
  return card("关于", "开源项目 · MIT License", [
    el("div", { style: "line-height:1.9;font-size:12px;color:var(--text-1)" }, [
      el("div", { text: "本项目完全在 Windows 标准窗口消息与辅助感知范围内工作：不注入 DLL、不反编译、不修改微信协议。" }),
      el("div", { text: "安全边界：白名单是唯一硬性保证；群聊识别使用名称启发式规则，无法 100% 拦截，请保持白名单模式开启。" }),
    ]),
    el("div", { class: "row-inline", style: "margin-top:12px" }, [
      button("打开项目主页", () => void window.engine.openExternal(REPO_URL)),
      button("查看 issue", () => void window.engine.openExternal(`${REPO_URL}/issues`)),
      el("div", { style: "flex:1" }),
      button("退出应用", async () => {
        const confirmed = await ctx.confirm({
          title: "退出应用？",
          body: "守护进程会随应用一起停止，微信将不再自动回复。",
          confirmLabel: "退出",
          danger: true,
        });
        if (confirmed) await window.engine.window("close");
      }, "danger"),
    ]),
  ]);
}
