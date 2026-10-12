import type { AppContext, PageDefinition } from "../context";
import { pageHead } from "../context";
import { banner, button, card, el, field, numberInput, tag, textInput } from "../ui";

/**
 * AI engine settings.
 *
 * The 1.x GUI exposed only base URL, key and model, and hard-coded temperature and the
 * 12 s request timeout. Those are real knobs for local models (a 7B model on a laptop
 * needs both), so they are editable here and validated by the engine.
 */

const PRESETS = [
  { label: "DeepSeek", base: "https://api.deepseek.com/v1", model: "deepseek-chat" },
  { label: "通义千问 Qwen", base: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "qwen-plus" },
  { label: "Kimi (Moonshot)", base: "https://api.moonshot.cn/v1", model: "moonshot-v1-8k" },
  { label: "智谱 GLM", base: "https://open.bigmodel.cn/api/paas/v4", model: "glm-4-flash" },
  { label: "OpenAI", base: "https://api.openai.com/v1", model: "gpt-4o-mini" },
  { label: "Ollama (本地)", base: "http://127.0.0.1:11434/v1", model: "qwen2.5:7b" },
  { label: "LM Studio (本地)", base: "http://127.0.0.1:1234/v1", model: "local-model" },
];

let apiKeyVisible = false;
let testResult: { ok: boolean; text: string; kind: "ok" | "warn" | "danger" } | null = null;

export const aiEnginePage: PageDefinition = {
  id: "ai-engine",
  label: "AI 引擎",
  glyph: "⚙",
  group: "配置",
  badge(ctx) {
    const key = ctx.config?.openai_api?.api_key;
    return key ? null : { text: "!", kind: "warn" as const };
  },
  render(ctx) {
    const api = ctx.config?.openai_api;
    if (!api) return el("div", { class: "page" }, [pageHead("AI 引擎", "正在加载配置…")]);

    const keyField = el("div", { class: "row" }, [
      el("div", { class: "grow", style: "flex:1" }, [
        textInput(api.api_key, {
          type: apiKeyVisible ? "text" : "password",
          placeholder: "sk-…",
          onInput: (value) => { pendingKey = value; },
        }),
      ]),
      button(apiKeyVisible ? "隐藏" : "显示", () => { apiKeyVisible = !apiKeyVisible; ctx.rerender(); }, "ghost", "btn--sm"),
      button("粘贴", async () => {
        try {
          const text = await navigator.clipboard.readText();
          pendingKey = text.trim();
          ctx.rerender();
        } catch {
          ctx.toast("warn", "无法读取剪贴板", "请手动粘贴 API Key。");
        }
      }, "ghost", "btn--sm"),
    ]);

    const rows: Node[] = [];

    rows.push(banner(
      "info",
      "支持任意 OpenAI 兼容接口",
      "协议与 OpenAI /chat/completions 一致即可：官方 API、中转站、本地 Ollama、LM Studio、vLLM 都能直接使用。",
    ));

    if (testResult) {
      rows.push(banner(testResult.kind, testResult.ok ? "接口配置检查通过" : "接口配置有问题", testResult.text));
    }

    rows.push(field("API Base URL", textInput(api.api_base, {
      placeholder: "https://api.deepseek.com/v1",
      onInput: (value) => { pendingBase = value; },
    }), "可填写到 /v1 为止，也可直接填写完整的 /chat/completions 地址。"));

    rows.push(field("API Key", keyField, "密钥只会保存在本机配置文件中，界面上默认隐藏。"));

    rows.push(field("模型 ID", textInput(api.model, {
      placeholder: "deepseek-chat",
      onInput: (value) => { pendingModel = value; },
    }), "例如 deepseek-chat、gpt-4o-mini、qwen2.5:7b、glm-4-flash。"));

    rows.push(el("div", { class: "sep" }));

    rows.push(el("div", { class: "row-inline" }, [
      el("span", { class: "muted", text: "快速填充：" }),
      ...PRESETS.map((preset) =>
        button(preset.label, () => {
          pendingBase = preset.base;
          pendingModel = preset.model;
          void ctx.invokeConfig({ openai_api: { ...api, api_base: preset.base, model: preset.model } });
          ctx.toast("ok", `已切换到 ${preset.label}`, `记得填写对应的 API Key。`);
          ctx.rerender();
        }, "ghost", "btn--sm"),
      ),
    ]));

    rows.push(el("div", { class: "sep" }));

    rows.push(el("div", { class: "grid grid--3" }, [
      el("div", {}, [
        el("label", { style: "font-size:11.5px;color:var(--text-1);display:block;margin-bottom:5px", text: `温度 (temperature)：${api.temperature}` }),
        el("input", {
          type: "range", min: "0", max: "1.5", step: "0.05", value: String(api.temperature),
          onchange: async (event: Event) => {
            const value = Number((event.target as HTMLInputElement).value);
            await ctx.invokeConfig({ openai_api: { ...ctx.config!.openai_api, temperature: value } });
            ctx.rerender();
          },
        }),
        el("div", { class: "hint", text: "越高越随机活泼，越低越稳定保守。日常闲聊 0.7～0.9，商务沟通 0.3～0.5。" }),
      ]),
      field("请求超时（秒）", numberInput(api.timeout_seconds ?? 12, {
        min: 1, max: 300, step: 1,
        onChange: async (value) => {
          await ctx.invokeConfig({ openai_api: { ...ctx.config!.openai_api, timeout_seconds: value } });
          ctx.toast("ok", "已保存超时时间");
        },
      }), "本地小模型首次加载较慢，可提高到 60 秒。"),
      field("失败重试次数", numberInput(api.max_retries ?? 2, {
        min: 0, max: 8, step: 1,
        onChange: async (value) => {
          await ctx.invokeConfig({ openai_api: { ...ctx.config!.openai_api, max_retries: value } });
          ctx.toast("ok", "已保存重试次数");
        },
      }), "仅对 429 / 5xx / 网络错误重试，会遵循服务端 Retry-After。"),
    ]));

    rows.push(el("div", { class: "sep" }));

    const actions = el("div", { class: "row-inline" }, [
      button("保存 API 配置", async () => {
        await ctx.invokeConfig({
          openai_api: {
            ...ctx.config!.openai_api,
            api_key: pendingKey ?? ctx.config!.openai_api.api_key,
            api_base: pendingBase ?? ctx.config!.openai_api.api_base,
            model: pendingModel ?? ctx.config!.openai_api.model,
          },
        });
        pendingKey = pendingBase = pendingModel = null;
        ctx.toast("ok", "配置已保存", "守护进程会在下一个巡检周期自动应用。");
        ctx.rerender();
      }, "primary"),
      button("检查配置", async () => {
        const result = await ctx.invoke("test_api");
        if (result.ok) {
          testResult = { ok: true, kind: "ok", text: `地址：${result.endpoint}\n模型：${result.model}\n密钥：${result.keyPreview}` };
        } else {
          testResult = { ok: false, kind: "danger", text: (result.problems ?? [result.reason ?? "未知问题"]).join("\n") };
        }
        ctx.rerender();
      }),
      button("真实调用测试", async () => {
        const result = await ctx.invoke("sandbox_test", { sender: "配置检查", message: "你好，请回复“连接正常”。" });
        if (result.ok) {
          testResult = { ok: true, kind: "ok", text: `模型回复：${result.reply}\n耗时：${result.elapsed} 秒` };
        } else {
          testResult = { ok: false, kind: "danger", text: String(result.message ?? result.reason ?? "调用失败") };
        }
        ctx.rerender();
      }),
    ]);
    rows.push(actions);

    return el("div", { class: "page" }, [
      pageHead("AI 引擎", "配置用于生成回复的大模型接口。任何兼容 OpenAI 协议的服务都可以直接使用。"),
      card("接口配置", "协议：OpenAI /chat/completions", rows, api.api_key ? tag("已配置密钥", "ok") : tag("缺少密钥", "warn")),
      renderRuntimeCard(ctx),
    ]);
  },
};

let pendingKey: string | null = null;
let pendingBase: string | null = null;
let pendingModel: string | null = null;

function renderRuntimeCard(ctx: AppContext): HTMLElement {
  const status = ctx.status;
  const ocr = status?.ocrStats;
  const ai = status?.aiStats;

  const rows: Node[] = [];
  rows.push(el("div", { class: "grid grid--3" }, [
    el("div", { class: "metric" }, [
      el("div", { class: "metric__label", text: "OCR 引擎" }),
      el("div", { class: "metric__value", text: ocr?.ready ? "就绪" : ocr?.error ? "失败" : "未加载" }),
      el("div", { class: "hint", text: ocr?.bundledModels ? "使用内置 PP-OCR 模型" : "使用 rapidocr 默认模型" }),
    ]),
    el("div", { class: "metric" }, [
      el("div", { class: "metric__label", text: "平均识别耗时" }),
      el("div", { class: "metric__value", text: `${Math.round((ocr?.averageSeconds ?? 0) * 1000)}`, unit: "ms" }),
      el("div", { class: "hint", text: `累计识别 ${ocr?.inferences ?? 0} 次` }),
    ]),
    el("div", { class: "metric" }, [
      el("div", { class: "metric__label", text: "大模型请求" }),
      el("div", { class: "metric__value", text: String(ai?.requests ?? 0), unit: "次" }),
      el("div", { class: "hint", text: `失败 ${ai?.failures ?? 0} · 重试 ${ai?.retries ?? 0} · 累计 ${(ai?.totalSeconds ?? 0).toFixed(1)} 秒` }),
    ]),
  ]));

  if (ocr?.error) {
    rows.push(banner("danger", "OCR 引擎不可用", `${ocr.error}\n提示：运行 npm run setup 或手动执行 pip install -r requirements.txt。`));
  }

  const rows2: Node[] = [
    ...rows,
    el("div", { class: "row-inline", style: "margin-top:12px" }, [
      button("预热 OCR 引擎", async () => {
        const result = await ctx.invoke("ocr_warmup");
        if (result.ok) ctx.toast("ok", "OCR 已就绪", "后续首次识别不再有冷启动延迟。");
        else ctx.toast("error", "OCR 加载失败", String(result.reason ?? ""));
        await ctx.refresh(true);
        ctx.rerender();
      }),
      el("span", { class: "muted", text: "首次识别需要加载约 15MB 的 ONNX 模型，预热可以消除第一次扫描的延迟。" }),
    ]),
  ];

  return card("引擎运行状态", "由 Python 引擎实时上报", rows2);
}
