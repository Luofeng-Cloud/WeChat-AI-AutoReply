import type { PageDefinition } from "../context";
import { pageHead } from "../context";
import { banner, button, card, el, emptyState, select, tag, textInput } from "../ui";

/**
 * Per-friend personas.
 *
 * The engine has read friend_personas since 1.0 and the shipped example config documents
 * it, but the GUI never exposed an editor, so the feature was unreachable and every save
 * silently erased it. This page is the missing control, and it is backed by a whitelist
 * picker so the key always matches a contact the engine can actually see.
 */

let newContact = "";
let editingKey: string | null = null;
let editingText = "";

const TEMPLATES: Array<{ label: string; text: string }> = [
  { label: "死党互损", text: "死党互损模式：说话幽默随和、接地气、开玩笑、互怼互损，称呼自然，绝不客套拘谨。" },
  { label: "知心朋友", text: "知心朋友模式：语气温柔活泼、偶尔俏皮、关心对方、真诚幽默。" },
  { label: "商务礼貌", text: "商务礼貌模式：专业得体、谦逊礼貌、响应迅速、办事靠谱。" },
  { label: "暧昧推拉", text: "温柔幽默模式：体贴细心、情商高、自然幽默、随和温暖，带一点暧昧推拉感。" },
  { label: "家人关怀", text: "家人关怀模式：语气亲切温暖、耐心体贴，多关心生活起居，说话朴实不浮夸。" },
  { label: "默认通用", text: "真人好友模式：语气自然随和、口语化、真诚得体。" },
];

export const personasPage: PageDefinition = {
  id: "personas",
  label: "好友人设",
  glyph: "👥",
  group: "配置",
  badge(ctx) {
    const count = Object.keys(ctx.config?.friend_personas ?? {}).length;
    return count ? { text: String(count) } : null;
  },
  dispose() {
    editingKey = null;
    editingText = "";
  },
  render(ctx) {
    const config = ctx.config;
    if (!config) return el("div", { class: "page" }, [pageHead("好友人设", "正在加载…")]);

    const personas = config.friend_personas ?? {};
    const rows: Node[] = [];

    rows.push(banner(
      "info",
      "为特定好友叠加专属语气",
      "专属人设会追加到全局提示词中。名字需要与白名单完全一致（引擎匹配到的是白名单里的写法），否则不会生效。",
    ));

    // -- add ---------------------------------------------------------------
    const whitelist = config.whitelist;
    const candidates = whitelist.filter((name) => !(name in personas));
    const picker = el("div", { class: "row" }, [
      el("div", { class: "grow", style: "flex:1" }, [
        candidates.length > 0
          ? select(newContact || candidates[0], [
              { value: "", label: "选择白名单中的好友…" },
              ...candidates.map((name) => ({ value: name, label: name })),
            ], (value) => { newContact = value; })
          : textInput(newContact, { placeholder: "手动输入好友备注名", onInput: (value) => { newContact = value; } }),
      ]),
      button("添加", async () => {
        const name = (newContact || candidates[0] || "").trim();
        if (!name) {
          ctx.toast("warn", "请先选择或输入好友名");
          return;
        }
        if (name in personas) {
          ctx.toast("warn", "该好友已有人设");
          return;
        }
        await ctx.invokeConfig({ friend_personas: { ...personas, [name]: TEMPLATES[0].text } });
        newContact = "";
        ctx.toast("ok", `已为「${name}」添加人设`, "点击下方卡片可编辑内容。");
        ctx.rerender();
      }, "primary"),
    ]);
    rows.push(el("div", { class: "field" }, [
      el("label", { text: "新增好友人设" }),
      picker,
      candidates.length === 0 && whitelist.length > 0
        ? el("div", { class: "hint", text: "白名单中的好友都已配置专属人设。" })
        : whitelist.length === 0
          ? el("div", { class: "hint", text: "白名单为空。建议先在「白名单与防扰」中添加好友，再回到这里配置人设。" })
          : null,
    ]));

    rows.push(el("div", { class: "sep" }));

    const keys = Object.keys(personas);
    if (keys.length === 0) {
      rows.push(emptyState("还没有专属人设", "所有好友都会使用「人设与 Prompt」中的全局提示词。为某个好友添加专属人设后，对话会带上该好友特有的语气。",
        button("去配置白名单", () => ctx.navigate("firewall"), "ghost")));
    } else {
      for (const key of keys) {
        const inWhitelist = whitelist.includes(key);
        const isEditing = editingKey === key;

        const body: Node[] = [];
        if (isEditing) {
          body.push(el("textarea", {
            rows: "4",
            value: editingText,
            oninput: (event: Event) => { editingText = (event.target as HTMLTextAreaElement).value; },
          }));
          body.push(el("div", { class: "row-inline", style: "margin-top:8px" }, [
            el("span", { class: "muted", text: "快速模板：" }),
            ...TEMPLATES.map((template) =>
              button(template.label, () => {
                editingText = template.text;
                ctx.rerender();
              }, "ghost", "btn--sm")),
          ]));
          body.push(el("div", { class: "row-inline", style: "margin-top:10px" }, [
            button("保存", async () => {
              await ctx.invokeConfig({ friend_personas: { ...personas, [key]: editingText.trim() } });
              editingKey = null;
              ctx.toast("ok", `「${key}」的人设已保存`);
              ctx.rerender();
            }, "primary", "btn--sm"),
            button("取消", () => {
              editingKey = null;
              ctx.rerender();
            }, "ghost", "btn--sm"),
          ]));
        } else {
          body.push(el("div", { style: "color:var(--text-1);line-height:1.75;font-size:12px;white-space:pre-wrap", text: personas[key] || "（空）" }));
          body.push(el("div", { class: "row-inline", style: "margin-top:10px" }, [
            inWhitelist ? tag("在白名单中", "ok") : tag("不在白名单中", "warn"),
            el("div", { style: "flex:1" }),
            button("编辑", () => {
              editingKey = key;
              editingText = personas[key] ?? "";
              ctx.rerender();
            }, "ghost", "btn--sm"),
            button("删除", async () => {
              const next = { ...personas };
              delete next[key];
              await ctx.invokeConfig({ friend_personas: next });
              ctx.toast("info", `已删除「${key}」的人设`);
              ctx.rerender();
            }, "danger", "btn--sm"),
          ]));
        }

        rows.push(card(key, inWhitelist ? "匹配白名单项，人设会生效" : "该好友不在白名单中，人设不会起作用", body,
          inWhitelist ? tag("生效中", "ok") : tag("未生效", "warn")));
      }
    }

    return el("div", { class: "page" }, [
      pageHead("好友人设", "为不同好友定制专属语气：死党互怼、商务礼貌、家人关怀，无缝贴合你本人的口吻。"),
      ...rows,
    ]);
  },
};
