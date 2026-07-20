/** A shell-free, sequential Pi tool for browser-only eval runs. */
import { spawn } from "node:child_process";
import { Type } from "typebox";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

type BridgeResult = {
	ok: boolean;
	output: string;
	details?: Record<string, unknown>;
};

function invokeBridge(command: string, signal?: AbortSignal): Promise<BridgeResult> {
	return new Promise((resolve) => {
		const python = process.env.EBROWSE_EVAL_PYTHON;
		if (!python) {
			resolve({
				ok: false,
				output: "error: EBROWSE_EVAL_PYTHON is not configured",
				details: { error_class: "policy_setup" },
			});
			return;
		}
		const child = spawn(python, ["-m", "ebrowse_evals.pi_tool", "--command", command], {
			env: process.env,
			shell: false,
			stdio: ["ignore", "pipe", "pipe"],
		});
		const stdout: Buffer[] = [];
		const stderr: Buffer[] = [];
		child.stdout.on("data", (chunk: Buffer) => stdout.push(chunk));
		child.stderr.on("data", (chunk: Buffer) => stderr.push(chunk));
		const abort = () => child.kill("SIGTERM");
		signal?.addEventListener("abort", abort, { once: true });
		child.on("error", (error) => {
			signal?.removeEventListener("abort", abort);
			resolve({
				ok: false,
				output: `error: could not launch browser policy tool: ${error.message}`,
				details: { error_class: "policy_setup" },
			});
		});
		child.on("close", () => {
			signal?.removeEventListener("abort", abort);
			const raw = Buffer.concat(stdout).toString("utf8").trim();
			try {
				resolve(JSON.parse(raw) as BridgeResult);
			} catch {
				const diagnostic = Buffer.concat(stderr).toString("utf8").trim();
				resolve({
					ok: false,
					output: `error: invalid browser policy response${diagnostic ? `: ${diagnostic}` : ""}`,
					details: { error_class: "policy_setup" },
				});
			}
		});
	});
}

export default function (pi: ExtensionAPI) {
	pi.registerTool({
		name: "ebrowse",
		label: "Browser",
		description:
			"Control the task browser with one ebrowse command, omitting the ebrowse prefix. Examples: {command:\"outline\"}, {command:\"fill @e3 \\\"hello world\\\"\"}. Commands are parsed as arguments; shell operators and expansion are not supported.",
		promptSnippet: "Navigate, inspect, and interact with the task website",
		promptGuidelines: [
			"Use the ebrowse tool for every browser interaction; omit the ebrowse prefix from its command parameter.",
		],
		parameters: Type.Object({
			command: Type.String(),
		}),
		executionMode: "sequential",
		async execute(_toolCallId, params, signal) {
			const result = await invokeBridge(params.command, signal);
			return {
				content: [{ type: "text", text: result.output }],
				details: result.details ?? {},
				isError: !result.ok,
			};
		},
	});
}
