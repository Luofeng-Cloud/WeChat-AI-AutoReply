import type { Detection } from "../../shared/protocol";
import type { AppContext, PageDefinition } from "../context";
import { pageHead } from "../context";
import { banner, button, card, el, emptyState, formatDuration, formatTime, metric, select, tag } from "../ui";

/**
 * Dashboard: live state of WeChat, the daemon, and what the perception layer currently
 * sees. The screenshot preview is the fastest way for a user to tell whether the engine
 * is locked onto the right conversation before allowing it to send anything.
 */

let previewEnabled = true;
let lastPreview: { dataUrl: string; at: number } | null = null;
let lastDetection: Detection | null = null;

function statusTag(ctx: AppContext): HTMLElement {
  const wechat = ctx.wechat;
  if (!wechat?.found) return tag("未检测到微信", "bad");
  if (wechat.offscreen) return tag("离屏挂载中", "warn");
  if (wechat.minimized) return tag("已最小化", "warn");
  return tag("已连接", "ok");
}

function renderWeChatCard(ctx: AppContext): HTMLElement {
  const wechat = ctx.wechat;
  const rows: Node[] = [];

  if (!wechat?.found) {
    rows.push(banner(
      "warn",
      "未检测到微信窗口",
      "请先登录并打开 PC 微信（支持 3.x 与 4.x）。引擎会持续探测，检测到窗口后自动开始工作。",
      button("尝试启动微信", async () => {
        const result = await ctx.invoke("wechat_open");
        if ((result as { ok?: boolean }).ok) ctx.toast("ok", "已请求启动微信");
        else ctx.toast("error", "启动微信失败", String((result as { reason?: string }).reason ?? ""));
        await ctx.refresh(true);
        ctx.rerender();
      }),
    ));
  }

  rows.push(el("div", { class: "grid grid--3" }, [
    metric("窗口句柄", wechat?.hwnd ? `0x${wechat.hwnd.toString(16).toUpperCase()}` : "—"),
    metric("微信版本", wechat?.version ? `${wechat.version}.x` : "—"),
    metric("DPI 缩放", wechat?.dpiScale ? `${wechat.dpiScale.toFixed(2)}×` : "—"),
    metric("窗口宽度", wechat?.rect ? String(wechat.rect[2] - wechat.rect[0]) : "—", "px"),
    metric("窗口高度", wechat?.rect ? String(wechat.rect[3] - wechat.rect[1]) : "—", "px"),
    metric("屏幕尺寸", wechat?.screen ? `${wechat.screen.width}×${wechat.screen.height}` : "—"),
  ]));

  const isHidden = Boolean(wechat?.offscreen);
  rows.push(el("div", { class: "row-inline", style: "margin-top:12px" }, [
    button(isHidden ? "召唤微信回桌面" : "隐藏微信至离屏", async () => {
      const result = await ctx.invoke("wechat_toggle_offscreen");
      const ok = Boolean((result as { ok?: boolean }).ok);
      if (ok) {
        ctx.toast("ok", isHidden ? "微信已还原到桌面" : "微信已移入离屏空间",
          isHidden ? "窗口已回到上次记录的位置。" : "桌面不再有微信窗口，守护进程仍可全速截图并回复。");
      } else {
        ctx.toast("error", "操作失败", String((result as { reason?: string }).reason ?? ""));
      }
      await ctx.refresh(true);
      ctx.rerender();
    }),
    button("最小化微信", async () => {
      await ctx.invoke("wechat_minimize");
      await ctx.refresh(true);
      ctx.rerender();
    }),
    button("保存当前截图", async () => {
      const shot = await ctx.invoke("capture_screenshot", { include_data_url: true });
      if (!shot.ok || !shot.dataUrl) {
        ctx.toast("error", "截图失败", String(shot.reason ?? ""));
        return;
      }
      const saved = await window.engine.saveScreenshot(shot.dataUrl, `wechat-${Date.now()}.png`);
      if ((saved as { ok?: boolean }).ok) ctx.toast("ok", "截图已保存", String((saved as { path?: string }).path));
      else if (!(saved as { canceled?: boolean }).canceled) ctx.toast("error", "保存失败", String((saved as { error?: string }).error ?? ""));
    }),
  ]));

  return card("微信客户端", "窗口状态与几何信息（所有坐标均为物理像素）", rows, statusTag(ctx));
}

function renderEngineCard(ctx: AppContext): HTMLElement {
  const status = ctx.status;
  const running = Boolean(status?.running);
  const paused = Boolean(status?.paused);
  const dryRun = Boolean(status?.dryRun);

  const head = el("div", { class: "row-inline" }, [
    button(running ? (paused ? "继续监听" : "暂停监听") : "启动守护", async () => {
      try {
        if (!running) await ctx.invoke("engine_start");
        else if (paused) await ctx.invoke("engine_resume");
        else await ctx.invoke("engine_pause");
        await ctx.refresh(true);
        ctx.rerender();
      } catch (error) {
        ctx.toast("error", "操作失败", String(error));
      }
    }, "primary"),
    button("停止守护", async () => {
      await ctx.invoke("engine_stop");
      await ctx.refresh(true);
      ctx.rerender();
    }, "danger"),
    button("立即扫描一次", async () => {
      const result = await ctx.invoke("engine_tick", { force: true });
      const detail = JSON.stringify(result);
      ctx.toast("info", "扫描完成", detail.length > 160 ? detail.slice(0, 160) + "…" : detail);
      await refreshDetection(ctx);
    }),
    button("清空对话记忆", async () => {
      const confirmed = await ctx.confirm({
        title: "清空对话记忆",
        body: "将清除引擎为止保存的每个好友的上下文历史。好友会感觉你“忘了”之前的聊天内容。此操作不影响黑名单与白名单。",
        confirmLabel: "清空",
        danger: true,
      });
      if (!confirmed) return;
      await ctx.invoke("engine_reset_memory", {});
      ctx.toast("ok", "已清空对话记忆");
    }),
  ]);

  const statusTagNode = running
    ? (paused ? tag("已暂停", "warn") : tag("监听中", "ok"))
    : tag("未运行", "default");

  const rows: Node[] = [];
  if (dryRun) {
    rows.push(banner(
      "warn",
      "演练模式已开启",
      "引擎会完整执行识别与大模型调用，但不会真的发出消息。适合首次配置时确认白名单与识别是否准确。",
    ));
  }
  if (status?.lastError) {
    rows.push(banner("danger", "最近一次错误", String(status.lastError)));
  }

  rows.push(el("div", { class: "grid grid--3" }, [
    metric("巡检周期", String(status?.cycles ?? 0), "次"),
    metric("已回复", String(status?.replies ?? 0), "条"),
    metric("记忆好友", String(status?.memoryContacts ?? 0), "位"),
    metric("运行时长", formatDuration(status?.startedAt ? Date.now() / 1000 - status.startedAt : 0)),
    metric("上次回复", formatTime(status?.lastReplyAt)),
    metric("大模型调用", String(status?.aiStats?.requests ?? 0), "次"),
  ]));

  if (status?.lastReplyTarget) {
    rows.push(el("div", { class: "list", style: "margin-top:12px" }, [
      el("div", { class: "list__item" }, [
        el("span", { class: "muted", text: "最近回复" }),
        el("span", { class: "grow", text: status.lastReplyTarget }),
        el("span", { class: "muted mono", text: status.lastReplyText ?? "" }),
      ]),
    ]));
  }

  rows.push(el("div", { class: "sep" }));
  rows.push(el("div", { class: "row-inline" }, [
    tag(dryRun ? "演练模式：开启" : "演练模式：关闭", dryRun ? "warn" : "default"),
    button(dryRun ? "关闭演练模式" : "开启演练模式", async () => {
      await ctx.invoke("engine_set_dry_run", { enabled: !dryRun });
      await ctx.refresh(true);
      ctx.rerender();
    }, "ghost", "btn--sm"),
    el("span", { class: "muted", text: "演练模式不会发送任何消息，但会真实调用大模型并记录识别结果。" }),
  ]));

  return card("AI 守护进程", "扫描、回复与熔断状态", [head, ...rows], statusTagNode);
}

async function refreshDetection(ctx: AppContext): Promise<void> {
  try {
    const result = await ctx.invoke("engine_preview");
    if (result.ok && result.detection) {
      lastDetection = result.detection as Detection;
      detectionError = null;
    } else {
      detectionError = String(result.reason ?? "未知原因");
    }
  } catch (error) {
    detectionError = String(error);
  }
}

let detectionError: string | null = null;

const REASON_LABELS: Record<string, string> = {
  "wechat-not-found": "未检测到微信窗口",
  "capture-failed": "无法截取微信窗口画面",
  "window-too-small": "微信窗口过小，无法识别",
  "ocr-empty": "OCR 未识别到任何文字",
};

function renderDetectionCard(ctx: AppContext): HTMLElement {
  const rows: Node[] = [];
  // Prefer the freshest analysis; fall back to the last one the daemon pushed so the
  // panel still has content right after a UI reload.
  const detection = lastDetection ?? ctx.status?.detection ?? null;

  if (detectionError) {
    rows.push(banner("info", "当前无法识别", REASON_LABELS[detectionError] ?? detectionError));
  }

  if (!detection) {
    rows.push(emptyState(
      "尚无识别结果",
      "点击“立即扫描一次”或开启守护进程后，这里会显示引擎当前锁定的会话、待回复消息与会话列表。",
    ));
    return card("识别结果", "引擎当前看到的内容", rows, tag("未采集", "default"));
  }

  const target = detection.target;
  rows.push(el("div", { class: "grid grid--3" }, [
    metric("当前会话", detection.title || "—"),
    metric("回复目标", target ?? "不回复", target ? "" : ""),
    metric("待回复条数", String(detection.pendingCount), "条"),
  ]));

  if (detection.pendingText) {
    rows.push(el("div", { class: "list", style: "margin-top:12px" }, [
      el("div", { class: "list__item" }, [
        el("span", { class: "muted", text: "待回复内容" }),
        el("span", { class: "grow", text: detection.pendingText }),
      ]),
    ]));
  }

  rows.push(el("div", { class: "sep" }));
  rows.push(el("div", { style: "font-size:11.5px;color:var(--text-2);margin-bottom:8px" }, [
    el("span", { text: `会话列表识别到 ${detection.rows.length} 个白名单/可回复会话` }),
  ]));

  if (detection.rows.length === 0) {
    rows.push(el("div", { class: "list" }, [
      el("div", { class: "list__empty", text: "会话列表中没有匹配到可回复的好友。请检查白名单设置。" }),
    ]));
  } else {
    rows.push(el("div", { class: "list" }, detection.rows.map((row) => el("div", { class: "list__item" }, [
      row.unread ? tag("未读", "warn") : tag("已读", "default"),
      row.active ? tag("当前", "ok") : null,
      el("span", { class: "grow", text: `${row.name}  →  ${row.canonical}` }),
      el("span", { class: "muted mono", text: row.tier }),
    ]))));
  }

  const confidence = target ? tag("已锁定目标", "ok") : tag("不会回复", "warn");
  return card("识别结果", `识别窗口 ${detection.width}×${detection.height} · 缩放 ${detection.scale.toFixed(2)}×`, rows, confidence);
}

function renderControlCard(ctx: AppContext): HTMLElement {
  const config = ctx.config;
  const rows: Node[] = [];

  rows.push(el("div", { class: "field" }, [
    el("label", { text: "注入方式（发送消息的技术路径）" }),
    select(config?.input_injection_mode ?? "auto", [
      { value: "auto", label: "自动（推荐）：优先后台注入，失败时回退前台" },
      { value: "background", label: "仅后台：完全不打断当前前台窗口" },
      { value: "foreground", label: "仅前台：兼容性最好，会短暂切换焦点" },
    ], async (value) => {
      await ctx.invokeConfig({ input_injection_mode: value as never });
      ctx.toast("ok", "注入方式已保存");
    }),
    el("div", { class: "hint", text: "后台注入通过 Win32 消息直接投递，鼠标与焦点完全不受影响；若某些微信版本不支持，自动模式会回退到前台粘贴，并在发送前强校验前台窗口，避免误发到其他程序。" }),
  ]));

  rows.push(el("div", { class: "field" }, [
    el("label", { text: `巡检间隔：${config?.check_interval_seconds ?? 0.8} 秒` }),
    el("input", {
      type: "range", min: "0.2", max: "5", step: "0.1",
      value: String(config?.check_interval_seconds ?? 0.8),
      onchange: async (event: Event) => {
        const value = Number((event.target as HTMLInputElement).value);
        await ctx.invokeConfig({ check_interval_seconds: value });
        ctx.rerender();
      },
    }),
    el("div", { class: "hint", text: "值越小响应越快，CPU 占用越高。0.5～1.0 秒适合绝大多数机器。" }),
  ]));

  return card("运行参数", "影响响应速度与打扰程度", rows);
}

function renderPreviewCard(ctx: AppContext): HTMLElement {
  const image = lastPreview
    ? el("img", { src: lastPreview.dataUrl, alt: "微信窗口实时截图" })
    : null;

  const body = image
    ? el("div", { class: "preview" }, [image])
    : el("div", { class: "preview" }, [
        el("div", { class: "preview__empty" }, [
          el("div", { text: previewEnabled ? "正在获取微信窗口画面…" : "实时预览已暂停" }),
          detectionError ? el("div", { text: REASON_LABELS[detectionError] ?? detectionError }) : null,
        ]),
      ]);

  const controls = el("div", { class: "row-inline" }, [
    button(previewEnabled ? "暂停预览" : "开启预览", () => {
      previewEnabled = !previewEnabled;
      if (!previewEnabled) lastPreview = null;
      ctx.rerender();
    }, "ghost", "btn--sm"),
    lastPreview ? el("span", { class: "muted", text: `更新于 ${new Date(lastPreview.at).toLocaleTimeString("zh-CN", { hour12: false })}` }) : null,
  ]);

  return card("实时预览", previewEnabled ? "每 1.2 秒抓取一次（使用 PrintWindow，离屏或被遮挡同样有效）" : "预览已暂停", [body], controls);
}

export const dashboardPage: PageDefinition = {
  id: "dashboard",
  label: "总览",
  glyph: "◎",
  group: "运行",
  badge(ctx) {
    const running = ctx.status?.running;
    if (!running) return { text: "停", kind: "warn" as const };
    return { text: String(ctx.status?.replies ?? 0), kind: "live" as const };
  },
  render(ctx) {
    // Reset page-local caches so navigating away and back shows fresh data.
    lastPreview = null;
    lastDetection = null;
    detectionError = null;

    const refreshPreview = async () => {
      if (!previewEnabled) return;
      try {
        const shot = await ctx.invoke("capture_data_url", { max_width: 720 });
        if (shot.ok && shot.dataUrl) {
          lastPreview = { dataUrl: shot.dataUrl, at: Date.now() };
          const image = stageImage();
          if (image) image.src = shot.dataUrl;
        }
      } catch {
        /* the detection panel reports the underlying reason */
      }
    };

    ctx.registerTimer("dashboard-preview", () => void refreshPreview(), 1200);
    ctx.registerTimer("dashboard-detection", () => void refreshDetection(ctx), 2500);
    void refreshPreview();
    void refreshDetection(ctx).then(() => ctx.rerender());

    const container = el("div", { class: "page" }, [
      pageHead("总览", "实时掌握微信窗口、守护进程与识别结果。所有检测均使用 PrintWindow 截图，微信被遮挡或离屏时依然有效。"),
      renderEngineCard(ctx),
      el("div", { class: "grid grid--split" }, [renderPreviewCard(ctx), renderWeChatCard(ctx)]),
      renderDetectionCard(ctx),
      renderControlCard(ctx),
    ]);
    return container;
  },
};

function stageImage(): HTMLImageElement | null {
  return document.querySelector<HTMLImageElement>(".preview img");
}
