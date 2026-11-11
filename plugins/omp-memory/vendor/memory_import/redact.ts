/**
 * Redaction of imported text: the Hindsight proxy's rules (redact_bridge.py calls hindsight_proxy.redact), then the
 * redaction omp itself applies to every memory write (`redactMemorySecrets`), so an imported row never holds
 * anything omp's own retain would have scrubbed.
 */
import path from "node:path";
import { redactMemorySecrets } from "./omp_redact";
import { bump, isRecord } from "./model";
import { fail } from "./sink";

const BRIDGE = path.join(import.meta.dir, "redact_bridge.py");
const CHUNK = 100;

export interface Redacted {
	text: string;
	counts: Record<string, number>;
	/** What the residual scan still finds after redaction: `[kind, offset]` (never the text). */
	residual: [string, number][];
	error: string | null;
}

interface BridgeResult {
	text?: string;
	counts?: Record<string, number>;
	residual?: [string, number][];
	error?: string;
}

export interface RedactOptions {
	python?: string;
}

async function runBridge(texts: string[], python: string): Promise<BridgeResult[]> {
	const child = Bun.spawn([python, BRIDGE], { stdin: "pipe", stdout: "pipe", stderr: "pipe" });
	child.stdin.write(JSON.stringify({ texts }));
	await child.stdin.end();
	const [out, , code] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited]);
	// the bridge's stderr is not shown: a traceback can quote the text it was given, which is not redacted yet
	if (code !== 0) fail("the redaction bridge failed", `exit status ${code}`);
	const parsed: unknown = JSON.parse(out);
	if (!isRecord(parsed) || !Array.isArray(parsed.results) || parsed.results.length !== texts.length) {
		fail("the redaction bridge returned an unexpected shape");
	}
	return parsed.results as BridgeResult[];
}

export async function redactTexts(texts: string[], options: RedactOptions = {}): Promise<Redacted[]> {
	const python = options.python ?? "python3";
	const out: Redacted[] = [];
	for (let start = 0; start < texts.length; start += CHUNK) {
		const chunk = texts.slice(start, start + CHUNK);
		const results = await runBridge(chunk, python);
		for (const [index, result] of results.entries()) {
			if (result.error !== undefined || result.text === undefined) {
				out.push({ text: chunk[index] ?? "", counts: {}, residual: [], error: result.error ?? "no text" });
				continue;
			}
			const counts = { ...(result.counts ?? {}) };
			const own = redactMemorySecrets(result.text);
			if (own !== result.text) bump(counts, "omp-redact");
			out.push({ text: own, counts, residual: result.residual ?? [], error: null });
		}
	}
	return out;
}
