/** Capture Pi's fully resolved system prompt for eval trace ingestion. */
import { appendFileSync } from "node:fs";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
	pi.on("agent_start", (_event, ctx) => {
		const path = process.env.EBROWSE_EVAL_SYSTEM_PROMPTS;
		if (!path) return;
		appendFileSync(
			path,
			JSON.stringify({ timestamp: Date.now(), systemPrompt: ctx.getSystemPrompt() }) + "\n",
			"utf8",
		);
	});
}
