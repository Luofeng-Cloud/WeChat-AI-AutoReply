/**
 * Tiny DOM toolkit.
 *
 * A framework would be a dependency to vendor and update for a UI this size; these
 * helpers cover everything the pages need in about a hundred lines, and they make the
 * "no innerHTML with user data" rule easy to follow, which matters because the UI renders
 * contact names and model output straight from the engine.
 */

type Attrs = Record<string, unknown>;

export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Attrs = {},
  children: Array<Node | string | null | undefined | false> = [],
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = String(value);
    else if (key === "text") node.textContent = String(value);
    else if (key === "html") node.innerHTML = String(value);
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value as EventListener);
    } else if (key === "value" && (node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement)) {
      // Textareas take their content from a child text node, not a value attribute, so
      // without this branch el("textarea", { value }) silently renders empty.
      node.value = String(value);
    } else if (key === "checked" && node instanceof HTMLInputElement) {
      node.checked = Boolean(value);
    } else if (key === "disabled") {
      (node as HTMLButtonElement).disabled = Boolean(value);
    } else if (key === "selected" && node instanceof HTMLOptionElement) {
      node.selected = Boolean(value);
    } else {
      node.setAttribute(key, String(value));
    }
  }
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    node.append(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

export function clear(node: HTMLElement): HTMLElement {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function card(title: string, subtitle: string, children: Node[], extra?: Node): HTMLElement {
  const head = el("div", { class: "card__head" }, [
    el("div", {}, [el("h2", { text: title }), subtitle ? el("p", { text: subtitle }) : null]),
    extra ?? null,
  ]);
  return el("div", { class: "card" }, [head, ...children]);
}

export function field(label: string, control: Node, hint?: string, extra?: Node): HTMLElement {
  return el("div", { class: "field" }, [
    el("label", { text: label }),
    extra ?? control,
    hint ? el("div", { class: "hint", text: hint }) : null,
  ]);
}

export function button(
  label: string,
  onClick: () => void,
  variant: "default" | "primary" | "danger" | "ghost" = "default",
  extraClass = "",
): HTMLButtonElement {
  const variantClass = variant === "default" ? "" : ` btn--${variant}`;
  return el("button", {
    class: `btn${variantClass}${extraClass ? " " + extraClass : ""}`,
    type: "button",
    text: label,
    onclick: (event: Event) => {
      event.preventDefault();
      onClick();
    },
  });
}

export function tag(text: string, kind: "default" | "ok" | "warn" | "bad" | "info" = "default"): HTMLElement {
  return el("span", { class: `tag${kind === "default" ? "" : ` tag--${kind}`}`, text });
}

export function metric(label: string, value: string, unit?: string): HTMLElement {
  return el("div", { class: "metric" }, [
    el("div", { class: "metric__label", text: label }),
    el("div", { class: "metric__value" }, [
      el("span", { text: value }),
      unit ? el("small", { text: unit }) : null,
    ]),
  ]);
}

export function switchRow(
  label: string,
  checked: boolean,
  onChange: (value: boolean) => void,
  hint?: string,
): HTMLElement {
  const input = el("input", { type: "checkbox", checked, onchange: (event: Event) => onChange((event.target as HTMLInputElement).checked) });
  return el("label", { class: "switch" }, [
    input,
    el("span", { class: "switch__track" }),
    el("span", { class: "switch__label", text: hint ? `${label} — ${hint}` : label }),
  ]);
}

export function textInput(value: string, options: { placeholder?: string; type?: string; onInput?: (value: string) => void; disabled?: boolean } = {}): HTMLInputElement {
  return el("input", {
    type: options.type ?? "text",
    value: value ?? "",
    placeholder: options.placeholder ?? "",
    disabled: options.disabled,
    oninput: options.onInput ? (event: Event) => options.onInput!((event.target as HTMLInputElement).value) : undefined,
  });
}

export function numberInput(
  value: number,
  options: { min?: number; max?: number; step?: number; onChange: (value: number) => void },
): HTMLInputElement {
  return el("input", {
    type: "number",
    value: String(value),
    min: options.min,
    max: options.max,
    step: options.step ?? 1,
    onchange: (event: Event) => {
      const parsed = Number((event.target as HTMLInputElement).value);
      options.onChange(Number.isFinite(parsed) ? parsed : value);
    },
  });
}

export function select<T extends string>(
  value: T,
  options: Array<{ value: T; label: string }>,
  onChange: (value: T) => void,
): HTMLSelectElement {
  return el(
    "select",
    { onchange: (event: Event) => onChange((event.target as HTMLSelectElement).value as T) },
    options.map((option) => el("option", { value: option.value, text: option.label, selected: option.value === value })),
  );
}

export function emptyState(title: string, body: string, action?: Node): HTMLElement {
  return el("div", { class: "empty" }, [el("h3", { text: title }), el("p", { text: body }), action ?? null]);
}

export function banner(
  kind: "info" | "warn" | "danger" | "ok",
  title: string,
  body: string,
  action?: Node,
): HTMLElement {
  return el("div", { class: `banner banner--${kind}` }, [
    el("div", { class: "grow" }, [el("strong", { text: title }), el("div", { text: body })]),
    action ?? null,
  ]);
}

export function editableList(
  items: string[],
  options: {
    placeholder: string;
    addLabel: string;
    emptyText: string;
    onAdd: (value: string) => void;
    onRemove: (value: string) => void;
    render?: (value: string) => Node;
  },
): HTMLElement {
  const input = textInput("", { placeholder: options.placeholder });
  const submit = () => {
    const value = input.value.trim();
    if (!value) return;
    options.onAdd(value);
    input.value = "";
  };
  input.addEventListener("keydown", (event) => {
    if ((event as KeyboardEvent).key === "Enter") submit();
  });

  const list = el("div", { class: "list" },
    items.length === 0
      ? [el("div", { class: "list__empty", text: options.emptyText })]
      : items.map((item) =>
          el("div", { class: "list__item" }, [
            el("div", { class: "grow" }, [options.render ? options.render(item) : document.createTextNode(item)]),
            button("移除", () => options.onRemove(item), "ghost", "btn--sm"),
          ]),
        ),
  );

  return el("div", {}, [
    list,
    el("div", { class: "row-inline", style: "margin-top:10px" }, [
      el("div", { class: "grow", style: "flex:1" }, [input]),
      button(options.addLabel, submit),
    ]),
  ]);
}

export function formatTime(seconds: number | null | undefined): string {
  if (!seconds) return "—";
  return new Date(seconds * 1000).toLocaleTimeString("zh-CN", { hour12: false });
}

export function formatDuration(seconds: number | null | undefined): string {
  if (!seconds) return "0 秒";
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours) return `${hours} 小时 ${minutes} 分`;
  if (minutes) return `${minutes} 分 ${secs} 秒`;
  return `${secs} 秒`;
}
