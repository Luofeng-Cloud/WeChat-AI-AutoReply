import type { SandboxResult } from "../../shared/protocol";
import type { PageDefinition } from "../context";
import { pageHead } from "../context";
import { banner, button, card, el, field, switchRow, tag, textInput } from "../ui";

/**
 * Sandbox: try the configured model without touching WeChat.
 *
 * Version 1.x did the request inline in the UI thread and had no way to inspect the
 * prompt. Here the exact system prompt is shown alongside the reply, and conversation
 * history can be included so a multi-turn persona can be judged.
 */

let sender = "测试好友";
let message = "在干嘛呢？晚上一起吃个饭不？";
let useHistory = false;
let running = false;
let result: SandboxResult | null = null;
let error: string | null = null;

export const sandboxPage: PageDefinition = {
  id: "sandbox",
  label: "回复沙盒",
  glyph: "🧪",
  group: "验证",
  badge(ctx) {
    if (!ctx.config?.openai_api?.api_key) return { text: "!", kind: "warn" as const };
    return null;
  },
  dispose() {
    result = null;
    error = null;
  },
  render(ctx) {
    const config = ctx.config;
    if (!config) return el("div", { class: "page" }, [pageHead("回复沙盒", "正在加载…")]);

    const hasKey = Boolean(config.openai_api?.api_key);
    const rows: Node[] = [];

    if (!hasKey) {
      rows.push(banner("warn", "尚未配置 API Key", "沙盒需要真实调用大模型。请先到「AI 引擎」填写 API Key。",
        button("去配置", () => ctx.navigate("ai-engine"), "ghost", "btn--sm")));
    }

    rows.push(el("div", { class: "grid grid--split" }, [
      field("好友昵称（决定使用哪套人设）", textInput(sender, {
        placeholder: "测试好友",
        onInput: (value) => { sender = value; },
      }), "若该昵称在「好友人设」中有配置，会一并应用。"),
      field("模型", el("div", { class: "mono muted", style: "padding:7px 0" }, [el("span", { text: config.openai_api.model || "未配置" })]),
        "在「AI 引擎」页面修改。"),
    ]));

    rows.push(field("好友发来的消息", el("textarea", {
      rows: "3",
      value: message,
      placeholder: "输入一句好友可能会发的话…",
      oninput: (event: Event) => { message = (event.target as HTMLTextAreaElement).value; },
    })));

    rows.push(el("div", { class: "row-inline" }, [
      switchRow("带上历史对话", useHistory, (value) => { useHistory = value; ctx.rerender(); }, "模拟连续对话的上下文"),
      el("div", { style: "flex:1" }),
      button(running ? "生成中…" : "触发回复测试", async () => {
        if (running) return;
        if (!message.trim()) {
          ctx.toast("warn", "请输入消息内容");
          return;
        }
        running = true;
        result = null;
        error = null;
        ctx.rerender();
        try {
          result = await ctx.invoke("sandbox_test", { sender: sender.trim() || "测试好友", message: message.trim(), use_history: useHistory });
          if (!result.ok) error = String(result.message ?? result.reason ?? "调用失败");
        } catch (caught) {
          error = String(caught);
        } finally {
          running = false;
          ctx.rerender();
        }
      }, "primary"),
    ]));

    rows.push(el("div", { class: "sep" }));

    if (error) {
      rows.push(banner("danger", "调用失败", error));
    }

    if (result?.ok) {
      rows.push(el("div", { class: "list" }, [
        el("div", { class: "list__item", style: "align-items:flex-start" }, [
          tag("AI 回复", "ok"),
          el("span", { class: "grow", style: "white-space:pre-wrap;line-height:1.75", text: result.reply ?? "" }),
        ]),
        el("div", { class: "list__item" }, [
          el("span", { class: "muted", text: `耗时 ${result.elapsed} 秒 · 模型 ${result.model} · 尝试 ${result.attempts} 次` }),
          el("div", { style: "flex:1" }),
          button("复制回复", async () => {
            try {
              await navigator.clipboard.writeText(result?.reply ?? "");
              ctx.toast("ok", "已复制到剪贴板");
            } catch {
              ctx.toast("warn", "无法写入剪贴板");
            }
          }, "ghost", "btn--sm"),
        ]),
      ]));

      const usage = result.usage as Record<string, unknown> | undefined;
      if (usage && Object.keys(usage).length > 0) {
        rows.push(el("div", { class: "hint", text: `Token 用量：${JSON.stringify(usage)}` }));
      }

      if (result.systemPrompt) {
        rows.push(el("div", { class: "sep" }));
        rows.push(el("div", { class: "field" }, [
          el("label", { text: "实际发送给模型的 system 提示词" }),
          el("pre", {
            class: "mono",
            style: "white-space:pre-wrap;word-break:break-word;background:var(--bg-2);border:1px solid var(--line);border-radius:7px;padding:10px 12px;margin:0;font-size:11.5px;line-height:1.75;max-height:260px;overflow:auto",
            text: result.systemPrompt,
          }),
        ]));
      }
    } else if (!error) {
      rows.push(el("div", { class: "empty" }, [
        el("h3", { text: "还没有测试结果" }),
        el("p", { text: "输入一条好友可能发来的消息，点击「触发回复测试」查看当前 Prompt 与模型组合下的真实回复质量。这里不会向微信发送任何内容。" }),
      ]));
    }

    return el("div", { class: "page" }, [
      pageHead("回复沙盒", "在不接触微信的前提下验证 Prompt 与模型效果。这是调整人设时最快的反馈回路。"),
      card("测试输入", "不会向微信发送任何消息", rows, running ? tag("生成中…", "info") : tag("就绪", "default")),
    ]);
  },
};
