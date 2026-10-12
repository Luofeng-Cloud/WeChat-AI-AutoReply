import type { PageDefinition } from "../context";
import { pageHead } from "../context";
import { button, card, el, select, tag } from "../ui";

/**
 * Live log viewer.
 *
 * Lines arrive as `engine.log` notifications, so this page needs no polling of its own.
 * The 1.x GUI re-read the log file with a character offset, which produced mojibake for
 * any non-ASCII line and lost everything after a "clear logs"; reading pushed
 * notifications sidesteps both, and the file remains available for external tooling.
 */

let filterLevel = "all";
let filterText = "";
let autoScroll = true;
let lines: Array<{ level: string; line: string; at: number }> = [];

export function appendLogLine(level: string, line: string): void {
  lines.push({ level, line, at: Date.now() });
  if (lines.length > 1500) lines = lines.slice(-1200);
}

function parseLine(raw: string): { level: string; body: string; time: string } {
  const match = /^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s?(.*)$/.exec(raw);
  const body = match ? match[2] : raw;
  const time = match ? match[1].slice(11) : "";
  let level = "info";
  if (/⚠️|\[发送失败\]|未通过|异常|熔断/.test(body)) level = "warn";
  if (/❌|错误|失败|error/i.test(body)) level = "error";
  if (/✅|成功|已回复/.test(body)) level = "success";
  if (/\[配置\]|\[OCR\]|\[engine\]/.test(body)) level = "debug";
  return { level, body, time };
}

export const logsPage: PageDefinition = {
  id: "logs",
  label: "运行日志",
  glyph: "📜",
  group: "运行",
  badge(ctx) {
    if (ctx.engineState.state === "crashed") return { text: "!", kind: "warn" as const };
    const recent = lines.filter((item) => Date.now() - item.at < 60000).length;
    return recent ? { text: String(recent) } : null;
  },
  render(ctx) {
    const rows: Node[] = [];

    const controls = el("div", { class: "row-inline" }, [
      select(filterLevel, [
        { value: "all", label: "全部级别" },
        { value: "info", label: "仅常规" },
        { value: "warn", label: "仅警告" },
        { value: "error", label: "仅错误" },
        { value: "success", label: "仅成功回复" },
      ], (value) => {
        filterLevel = value;
        ctx.rerender();
      }),
      el("input", {
        type: "text",
        placeholder: "按关键字过滤（好友名、错误码…）",
        value: filterText,
        style: "max-width:260px",
        oninput: (event: Event) => {
          filterText = (event.target as HTMLInputElement).value;
          renderBody();
        },
      }),
      el("div", { style: "flex:1" }),
      button(autoScroll ? "停止自动滚动" : "恢复自动滚动", () => {
        autoScroll = !autoScroll;
        if (autoScroll) renderBody();
        ctx.rerender();
      }, "ghost", "btn--sm"),
      button("打开日志文件", async () => {
        const result = await ctx.invoke("log_open");
        if (!result.ok) ctx.toast("warn", "无法打开日志文件", String(result.reason ?? ""));
      }, "ghost", "btn--sm"),
      button("清空", async () => {
        const confirmed = await ctx.confirm({
          title: "清空日志？",
          body: "日志文件与界面上的记录都会被清空。已发生的问题不会再显示，请先确认没有需要保留的信息。",
          confirmLabel: "清空",
          danger: true,
        });
        if (!confirmed) return;
        await ctx.invoke("log_clear");
        lines = [];
        renderBody();
        ctx.toast("ok", "日志已清空");
      }, "danger", "btn--sm"),
    ]);

    const view = el("div", { class: "log" });

    const matches = () => lines.filter((item) => {
      const parsed = parseLine(item.line);
      if (filterLevel !== "all" && parsed.level !== filterLevel) return false;
      if (filterText && !item.line.includes(filterText)) return false;
      return true;
    });

    function renderBody(): void {
      const visible = matches();
      if (visible.length === 0) {
        view.replaceChildren(el("div", { class: "muted", text: lines.length === 0 ? "暂无日志。启动守护进程后，识别与回复过程会实时显示在这里。" : "没有符合过滤条件的日志。" }));
        return;
      }
      view.replaceChildren(...visible.slice(-600).map((item) => {
        const parsed = parseLine(item.line);
        return el("div", { class: `log__line log__line--${parsed.level}` }, [
          el("span", { class: "log__time", text: parsed.time }),
          el("span", { class: "log__body", text: parsed.body }),
        ]);
      }));
      if (autoScroll) view.scrollTop = view.scrollHeight;
    }

    // Seed from the engine ring buffer so the page is useful immediately after a reload.
    void ctx.invoke("log_tail", { lines: 600 }).then((tail) => {
      if (tail.lines?.length && tail.lines.length > lines.length) {
        lines = tail.lines.map((item) => ({ level: item.level, line: item.line, at: item.at * 1000 }));
        renderBody();
      }
    }).catch(() => undefined);

    renderBody();
    ctx.registerTimer("logs-refresh", () => renderBody(), 900);

    rows.push(controls);
    rows.push(el("div", { style: "margin-top:10px" }, [view]));

    return el("div", { class: "page" }, [
      pageHead("运行日志", "实时显示识别、生成与发送的每一步。发生异常时这里是第一现场。"),
      card("日志流", `${lines.length} 条已缓冲 · 由引擎实时推送`, rows, tag(filterLevel === "all" ? "全部" : filterLevel, "info")),
    ]);
  },
};
