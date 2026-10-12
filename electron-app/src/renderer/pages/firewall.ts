import type { PageDefinition } from "../context";
import { pageHead } from "../context";
import { banner, button, card, el, editableList, switchRow, tag, textInput } from "../ui";

/**
 * Whitelist / blacklist control.
 *
 * The live matcher is the important addition: version 1.x let a user add a name and never
 * showed whether the engine would actually recognise it. Typing the name as it appears in
 * WeChat and seeing whether it matches (and at which confidence tier) turns a silent
 * failure into a two-second check.
 */

let probeName = "";
let probeResult: { matched: boolean; canonical: string; tier: string; isGroup: boolean } | null = null;

const TIER_LABELS: Record<string, string> = {
  exact: "完全一致",
  prefix: "前缀匹配（会话行带消息预览）",
  truncated: "容错匹配（OCR 少识别一个字符）",
  "ocr-fold": "形近字容错匹配",
  "ocr-prefix": "形近字前缀匹配",
  "ocr-truncated": "形近字容错匹配",
};

export const firewallPage: PageDefinition = {
  id: "firewall",
  label: "白名单与防扰",
  glyph: "🛡",
  group: "配置",
  badge(ctx) {
    if (!ctx.config?.whitelist_mode) return { text: "开放", kind: "warn" as const };
    return { text: String(ctx.config?.whitelist?.length ?? 0) };
  },
  dispose() {
    probeName = "";
    probeResult = null;
  },
  render(ctx) {
    const config = ctx.config;
    if (!config) return el("div", { class: "page" }, [pageHead("白名单与防扰", "正在加载…")]);

    const rows: Node[] = [];

    rows.push(switchRow(
      "白名单模式",
      config.whitelist_mode,
      async (value) => {
        if (!value) {
          const confirmed = await ctx.confirm({
            title: "关闭白名单模式？",
            body: "关闭后引擎会尝试回复所有「看起来像单人好友」的会话。群聊识别基于名称启发式规则，无法 100% 拦截，存在回复到群里的风险。建议只在测试时关闭。",
            confirmLabel: "我了解风险，关闭",
            danger: true,
          });
          if (!confirmed) {
            ctx.rerender();
            return;
          }
        }
        await ctx.invokeConfig({ whitelist_mode: value });
        ctx.toast(value ? "ok" : "warn", value ? "白名单模式已开启" : "白名单模式已关闭",
          value ? "只有名单内的好友会收到自动回复。" : "引擎现在会回复所有单人好友，请谨慎使用。");
        ctx.rerender();
      },
      config.whitelist_mode ? "仅回复名单内好友" : "当前会回复所有单人好友",
    ));

    if (!config.whitelist_mode) {
      rows.push(el("div", { style: "margin-top:10px" }, [
        banner("danger", "当前为开放模式", "群聊识别依赖名称启发式规则，无法保证 100% 拦截。生产使用请开启白名单模式。"),
      ]));
    }

    rows.push(el("div", { class: "sep" }));

    rows.push(el("div", { class: "field" }, [
      el("label", { text: "匹配测试：把微信里看到的名字原样输入，检查引擎能否识别" }),
      el("div", { class: "row" }, [
        el("div", { class: "grow", style: "flex:1" }, [
          textInput(probeName, {
            placeholder: "例如：张三   或   3张三（带未读红点数字）",
            onInput: (value) => { probeName = value; },
          }),
        ]),
        button("测试匹配", async () => {
          if (!probeName.trim()) {
            ctx.toast("warn", "请输入要测试的名字");
            return;
          }
          const result = await ctx.invoke("naming_check", { contact: probeName, whitelist: config.whitelist });
          probeResult = { matched: result.matched, canonical: result.canonical, tier: result.tier, isGroup: result.isGroup };
          ctx.rerender();
        }),
      ]),
      probeResult
        ? probeResult.isGroup
          ? el("div", { class: "hint", style: "color:var(--warn)" }, [el("span", { text: "该名称被判定为群聊，永远不会被回复。" })])
          : probeResult.matched
            ? el("div", { class: "hint", style: "color:var(--brand)" }, [
                el("span", { text: `匹配成功 → 白名单项「${probeResult.canonical}」（${TIER_LABELS[probeResult.tier] ?? probeResult.tier}）` }),
              ])
            : el("div", { class: "hint", style: "color:var(--danger)" }, [
                el("span", { text: "未匹配任何白名单项。请检查名字是否与微信中完全一致（含空格与符号）。" }),
              ])
        : null,
    ]));

    const lists = el("div", { class: "grid grid--split" }, [
      card("允许自动回复的白名单", "只有这里的名字会收到自动回复", [
        editableList(config.whitelist, {
          placeholder: "输入好友在微信中显示的备注名",
          addLabel: "添加好友",
          emptyText: "白名单为空 —— 当前不会回复任何人",
          onAdd: async (value) => {
            if (config.whitelist.includes(value)) {
              ctx.toast("warn", "该好友已在白名单中");
              return;
            }
            await ctx.invokeConfig({ whitelist: [...config.whitelist, value] });
            ctx.toast("ok", `已添加「${value}」`);
            ctx.rerender();
          },
          onRemove: async (value) => {
            await ctx.invokeConfig({ whitelist: config.whitelist.filter((item) => item !== value) });
            ctx.toast("info", `已移除「${value}」`);
            ctx.rerender();
          },
        }),
      ], tag(String(config.whitelist.length), "ok")),

      card("严格屏蔽黑名单", "群聊、服务号与任何不想回复的联系人", [
        editableList(config.blacklist, {
          placeholder: "输入要屏蔽的名称片段",
          addLabel: "添加屏蔽",
          emptyText: "黑名单为空",
          onAdd: async (value) => {
            if (config.blacklist.includes(value)) return;
            await ctx.invokeConfig({ blacklist: [...config.blacklist, value] });
            ctx.rerender();
          },
          onRemove: async (value) => {
            await ctx.invokeConfig({ blacklist: config.blacklist.filter((item) => item !== value) });
            ctx.rerender();
          },
        }),
        el("div", { class: "row-inline", style: "margin-top:10px" }, [
          button("恢复默认屏蔽项", async () => {
            const defaults = ctx.presets?.blacklistDefaults ?? [];
            const merged = [...new Set([...config.blacklist, ...defaults])];
            await ctx.invokeConfig({ blacklist: merged });
            ctx.toast("ok", "已恢复默认屏蔽项");
            ctx.rerender();
          }, "ghost", "btn--sm"),
        ]),
      ], tag(String(config.blacklist.length), "bad")),
    ]);

    rows.push(lists);

    return el("div", { class: "page" }, [
      pageHead("白名单与防扰", "白名单是唯一的硬性保证：只有名单内的好友会被回复。黑名单与群聊规则是额外的启发式保护。"),
      card("回复范围", "决定引擎可以对谁说话", rows, config.whitelist_mode ? tag("白名单模式", "ok") : tag("开放模式", "warn")),
    ]);
  },
};
