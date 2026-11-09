// Test-only model provider for omp: no network, no cost. A prompt containing RUN_SLEEP answers with a `bash` tool call
// running `sleep 60` (so a turn stays busy until it is stopped); anything else gets a short text reply.
// Every request is appended to $OMP_TETHER_TEST_DIR/provider-calls.jsonl. Load it with `omp -e <this file>` and pick
// the model `tethertest/lite`.
import * as fs from "node:fs";
import * as path from "node:path";
import {
	type Api,
	type AssistantMessage,
	createAssistantMessageEventStream,
	type AssistantMessageEventStream,
	type Context,
	type Model,
	type SimpleStreamOptions,
	type ToolCall,
} from "@oh-my-pi/pi-ai";
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";

const TEST_DIR = process.env.OMP_TETHER_TEST_DIR ?? "/tmp/omp-tether-test";
let calls = 0;

function lastUserText(messages: Context["messages"]): string {
	for (let i = messages.length - 1; i >= 0; i--) {
		const m = messages[i];
		if (m.role !== "user") continue;
		if (typeof m.content === "string") return m.content;
		return m.content.map(b => (b.type === "text" ? b.text : "")).join("");
	}
	return "";
}

function stream(model: Model<Api>, context: Context, options?: SimpleStreamOptions): AssistantMessageEventStream {
	const out = createAssistantMessageEventStream();
	const n = ++calls;
	const last = context.messages.at(-1);
	const text = lastUserText(context.messages);
	fs.appendFileSync(
		path.join(TEST_DIR, "provider-calls.jsonl"),
		`${JSON.stringify({ n, lastRole: last?.role ?? null, lastUserText: text.slice(0, 80) })}\n`,
	);
	const partial: AssistantMessage = {
		role: "assistant",
		content: [],
		api: model.api,
		provider: model.provider,
		model: model.id,
		usage: {
			input: 1,
			output: 1,
			cacheRead: 0,
			cacheWrite: 0,
			totalTokens: 2,
			cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
		},
		stopReason: "stop",
		timestamp: Date.now(),
	};
	if (options?.signal?.aborted) {
		partial.stopReason = "aborted";
		queueMicrotask(() => out.push({ type: "error", reason: "aborted", error: partial }));
		return out;
	}
	queueMicrotask(() => {
		out.push({ type: "start", partial });
		if (last?.role === "user" && text.includes("RUN_SLEEP")) {
			const call: ToolCall = { type: "toolCall", id: `call-${n}`, name: "bash", arguments: { command: "sleep 60" } };
			partial.content.push(call);
			out.push({ type: "toolcall_start", contentIndex: 0, partial });
			out.push({ type: "toolcall_delta", contentIndex: 0, delta: JSON.stringify(call.arguments), partial });
			out.push({ type: "toolcall_end", contentIndex: 0, toolCall: call, partial });
			partial.stopReason = "toolUse";
			out.push({ type: "done", reason: "toolUse", message: partial });
			return;
		}
		const reply = `test reply ${n}`;
		partial.content.push({ type: "text", text: reply });
		out.push({ type: "text_start", contentIndex: 0, partial });
		out.push({ type: "text_delta", contentIndex: 0, delta: reply, partial });
		out.push({ type: "text_end", contentIndex: 0, content: reply, partial });
		out.push({ type: "done", reason: "stop", message: partial });
	});
	return out;
}

export default function testProvider(pi: ExtensionAPI) {
	fs.mkdirSync(TEST_DIR, { recursive: true });
	pi.registerProvider("tethertest", {
		baseUrl: "https://tethertest.invalid/",
		apiKey: "tethertest-local-no-secret",
		api: "tethertest-api",
		streamSimple: stream,
		models: [
			{
				id: "lite",
				name: "Tether Test Lite",
				reasoning: false,
				input: ["text"],
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
				contextWindow: 200_000,
				maxTokens: 8_000,
			},
		],
	});
}
