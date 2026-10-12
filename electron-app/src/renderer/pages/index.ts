import type { PageDefinition } from "../context";
import { aiEnginePage } from "./ai-engine";
import { dashboardPage } from "./dashboard";
import { firewallPage } from "./firewall";
import { logsPage } from "./logs";
import { personasPage } from "./personas";
import { promptsPage } from "./prompts";
import { sandboxPage } from "./sandbox";
import { settingsPage } from "./settings";

/** Rail order. Grouping mirrors the mental model: run it, configure it, verify it, fix it. */
export const enginePages: PageDefinition[] = [
  dashboardPage,
  logsPage,
  aiEnginePage,
  promptsPage,
  firewallPage,
  personasPage,
  sandboxPage,
  settingsPage,
];
