// The pairing-store/1 conformance suite, run against this plugin's reader and writer, and against deliberately flawed
// implementations: each flaw must make exactly the checks that cover it fail (a suite that cannot fail proves nothing).
import { describe, expect, test } from "bun:test";
import * as path from "node:path";
import { runConformance } from "../conformance/pairing-store-suite";

const bun = process.execPath;
const here = import.meta.dir;
const real = [bun, path.join(here, "..", "conformance", "adapter.ts")];
const broken = [bun, path.join(here, "support", "broken-adapter.ts")];

async function failing(adapter: string[], flaw: string, only?: string[]): Promise<string[]> {
	process.env.BREAK = flaw;
	try {
		const results = await runConformance({ reader: adapter, writer: adapter }, only ? id => only.includes(id) : undefined);
		return results.filter(r => !r.ok).map(r => r.id);
	} finally {
		delete process.env.BREAK;
	}
}

describe("pairing-store/1 conformance", () => {
	test("this plugin's reader and writer pass every check", async () => {
		const results = await runConformance({ reader: real, writer: real });
		expect(results.filter(r => !r.ok)).toEqual([]);
		expect(results.length).toBeGreaterThanOrEqual(30);
	}, 120_000);

	test("the flawless baseline of the flawed adapter passes too (so every failure below is the flaw)", async () => {
		expect(await failing(broken, "")).toEqual([]);
	}, 120_000);

	const flaws: Array<[string, string[]]> = [
		["ignore-modes", ["R9", "R10"]],
		["follow-symlink", ["R11", "R12", "W9"]],
		["raw-keys", ["R5"]],
		["no-length-cap", ["R6"]],
		["first-cookie-only", ["R4"]],
		["chmod-on-read", ["R16"]],
		["array-ok", ["R14"]],
		["plain-token", ["W1"]],
		["short-token", ["W1"]],
		["loose-modes", ["W1", "W2"]],
		["reusable-token", ["W3"]],
		["accept-expired", ["W3"]],
		["no-label-clean", ["W4"]],
		["short-prefix", ["W6"]],
		["ambiguous-ok", ["W6"]],
		["overwrite-corrupt", ["W8"]],
		["no-tighten", ["W9"]],
		["temp-leftover", ["W10"]],
		["no-lock", ["W11"]],
		["empty-ok", ["W13"]],
		["lock-loose-ok", ["W14"]],
	];
	for (const [flaw, expected] of flaws) {
		test(`flaw "${flaw}" is caught by ${expected.join(", ")}`, async () => {
			const failed = await failing(broken, flaw, expected);
			for (const id of expected) expect(failed).toContain(id);
		}, 120_000);
	}
});
