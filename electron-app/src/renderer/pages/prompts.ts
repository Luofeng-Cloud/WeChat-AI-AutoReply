import type { AppContext, PageDefinition } from "../context";
import { pageHead } from "../context";
import { banner, button, card, el, tag } from "../ui";

/**
 * Persona and instruction editor.
 *
 * Preset switching preserves your own text in both directions: editing a stock preset
 * moves the edit into the custom slot, and selecting the custom preset restores it.
 * Version 1.x overwrote the edit without asking, which was the single most-lost piece of
 * user work in that release.
 */

let draft: string | null = null;
let draftDirty = false;

/** Body text of every stock preset, filled in on each render. */
let presetBodies: Array<[string, string]> = [];

/** Which stock preset a prompt body corresponds to, if any. */
function stockKeyFor(text: string): string | null {
  const trimmed = text.trim();
  const hit = presetBodies.find(([, body]) => body.trim() === trimmed);
  return hit ? hit[0] : null;
}

interface PresetContext {
  ctx: AppContext;
  key: string;
  active: boolean;
}

function presetButton({ ctx, key, active }: PresetContext): HTMLButtonElement {
  const node = el("button", {
    class: active ? "btn btn--sm btn--primary" : "btn btn--sm btn--ghost",
    type: "button",
    text: key,
  });
  node.addEventListener("click", async () => {
    if (active) return;
    const applied = await ctx.invoke("apply_preset", { key });
    ctx.config = applied;
    draft = null;
    draftDirty = false;
    ctx.toast("ok", `已切换到「${key}」`);
    ctx.rerender();
  });
  return node;
}

export const promptsPage: PageDefinition = {
  id: "prompts",
  label: "人设与 Prompt",
  glyph: "🎭",
  group: "配置",
  badge(ctx) {
    const preset = ctx.config?.prompt_preset;
    return preset ? { text: preset.slice(0, 1) } : null;
  },
  dispose() {
    draft = null;
    draftDirty = false;
  },
  render(ctx) {
    const config = ctx.config;
    const presets = ctx.presets;
    if (!config || !presets) {
      return el("div", { class: "page" }, [pageHead("人设与 Prompt", "正在加载…")]);
    }

    presetBodies = Object.entries(presets.prompts);
    const currentPreset = config.prompt_preset;
    const isCustom = currentPreset === presets.customKey;
    const text = draft ?? config.system_prompt;

    const counter = el("span", { class: "muted", text: `已输入 ${[...text].length} 字` });

    const textarea = el("textarea", {
      rows: "14",
      value: text,
      placeholder: "在这里描述 AI 应该如何说话…",
    });
    textarea.addEventListener("input", () => {
      draft = textarea.value;
      draftDirty = draft.trim() !== config.system_prompt.trim();
      counter.textContent = `已输入 ${[...draft].length} 字`;
    });

    const rows: Node[] = [];

    rows.push(banner(
      "info",
      "提示词决定语气，好友人设决定对谁用什么语气",
      "这里是全局人设。若要针对某个好友单独设置（例如对老板正式、对死党互怼），请到「好友人设」页面为具体好友添加专属描述，它会自动叠加到当前提示词之上。",
    ));

    const presetRow = el("div", { class: "row-inline" },
      presets.order.map((key) => presetButton({ ctx, key, active: key === currentPreset })),
    );
    rows.push(el("div", { class: "field" }, [el("label", { text: "预设人设风格" }), presetRow]));

    const statusRow = el("div", { class: "row-inline", style: "margin-top:8px" }, [
      counter,
      el("div", { class: "spacer", style: "flex:1" }),
      draftDirty ? tag("有未保存修改", "warn") : tag("已保存", "ok"),
    ]);
    const editor = el("div", { class: "field" }, [
      el("label", { text: isCustom ? "自定义提示词（可自由编辑）" : "提示词内容（编辑后会自动转为自定义）" }),
      textarea,
      statusRow,
    ]);
    rows.push(editor);

    const saveButton = button("保存提示词", async () => {
      const value = textarea.value.trim();
      if (!value) {
        ctx.toast("warn", "提示词不能为空");
        return;
      }
      const stockKey = stockKeyFor(value);
      await ctx.invokeConfig({
        system_prompt: value,
        custom_prompt: stockKey ? config.custom_prompt : value,
        prompt_preset: stockKey ?? presets.customKey,
      });
      draft = null;
      draftDirty = false;
      ctx.toast("ok", "提示词已保存", "下一个巡检周期起对新消息生效。");
      ctx.rerender();
    }, "primary");

    const revertButton = button("恢复为当前预设", async () => {
      const confirmed = await ctx.confirm({
        title: "放弃当前编辑？",
        body: "编辑框会恢复为当前选中预设的原始内容，未保存的修改会丢失。",
        confirmLabel: "恢复",
        danger: true,
      });
      if (!confirmed) return;
      draft = null;
      draftDirty = false;
      ctx.rerender();
    }, "ghost");

    const inspectButton = button("查看实际发送给模型的内容", async () => {
      const preview = await ctx.invoke("sandbox_preview_prompt", { sender: "示例好友" });
      const body = el("pre", {
        class: "mono",
        style: "white-space:pre-wrap;word-break:break-word;margin:0;font-size:11.5px;line-height:1.75",
        text: preview.systemPrompt,
      });
      await ctx.modal({ title: "实际请求中的 system 提示词", body: "", content: body });
    }, "ghost");

    rows.push(el("div", { class: "row-inline" }, [saveButton, revertButton, inspectButton]));

    return el("div", { class: "page" }, [
      pageHead("人设与 Prompt", "决定 AI 用什么样的口吻、多长的句子、以什么身份与好友对话。"),
      card(
        "人设提示词",
        `${presets.order.length} 个内置风格 · 当前：${currentPreset}`,
        rows,
        isCustom ? tag("自定义", "info") : tag("内置预设", "default"),
      ),
    ]);
  },
};
