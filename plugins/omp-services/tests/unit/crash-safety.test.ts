// A throw inside a callback that omp does not wrap surfaces as an uncaughtException, which omp treats as fatal for the
// whole session. This audits the extension's source for such callbacks and for anything it must never do.
import { describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as path from "node:path";

const SOURCE = path.resolve(import.meta.dir, "../../src/omp-services.ts");

/** Source with comments and string literals blanked, so a regex cannot match prose. Offsets are preserved. */
function code(text: string): string {
	return text
		.replace(/\/\*[\s\S]*?\*\//g, m => m.replace(/[^\n]/g, " "))
		.replace(/\/\/[^\n]*/g, m => " ".repeat(m.length))
		.replace(/`(?:[^`\\]|\\.)*`/g, m => m[0] + " ".repeat(m.length - 2) + m[0])
		.replace(/"(?:[^"\\\n]|\\.)*"/g, m => `"${" ".repeat(m.length - 2)}"`);
}

function callText(original: string, stripped: string, open: number): string {
	let depth = 0;
	for (let i = open; i < stripped.length; i++) {
		if (stripped[i] === "(") depth++;
		else if (stripped[i] === ")" && --depth === 0) return original.slice(open, i + 1);
	}
	return original.slice(open);
}

export function auditSource(original: string): string[] {
	const stripped = code(original);
	const problems: string[] = [];
	const snippet = (index: number) => original.slice(index, index + 60).replace(/\s+/g, " ");
	for (const m of stripped.matchAll(/(^|[^.\w$])(setTimeout|setInterval|setImmediate|queueMicrotask)\s*\(|process\s*\.\s*(nextTick|on|once|addListener)\s*\(|\b\w+\s*\.\s*setTimeout\s*\(/g)) {
		if (/\b(ctx|c)\s*\.\s*setTimeout\s*\(/.test(m[0])) continue;
		problems.push(`raw timer or hook: ${snippet(m.index)}`);
	}
	for (const m of stripped.matchAll(/\b[\w.?]+\s*\.\s*(?:on|once|listen|watch)\s*\(|\bchildProcess\s*\.\s*(?:execFile|spawn|exec)\s*\(/g)) {
		const text = callText(original, stripped, m.index + m[0].length - 1);
		if (!text.includes("guarded(")) problems.push(`unwrapped callback registration: ${snippet(m.index)}`);
	}
	for (const m of stripped.matchAll(/\bfs\s*\.\s*(?!constants\b)(\w+)\s*\(/g)) {
		if (!(m[1] as string).endsWith("Sync")) problems.push(`callback-style fs call: ${snippet(m.index)}`);
	}
	for (const m of stripped.matchAll(/\.\s*then\s*\(/g)) problems.push(`promise chain: ${snippet(m.index)}`);
	if (/\bvoid\s+\w+\(/.test(stripped)) problems.push("floating promise");
	return problems;
}

describe("raw-callback audit of src/omp-services.ts", () => {
	const original = fs.readFileSync(SOURCE, "utf8");
	const stripped = code(original);

	test("no unwrapped callback, raw timer, floating promise or callback-style fs call", () => {
		expect(auditSource(original)).toEqual([]);
	});

	test("the audit itself catches each class of mistake", () => {
		const bad: Array<[string, string]> = [
			["a raw timer", "function f() { setTimeout(() => {}, 5); }"],
			["an unwrapped exec callback", "childProcess.execFile('x', [], {}, (e, o) => {});"],
			["an unwrapped handler", "child.on('close', () => {});"],
			["an async fs callback", "fs.readFile(p, () => {});"],
			["a promise chain", "p.then(() => {});"],
			["a process hook", "process.on('exit', () => {});"],
		];
		for (const [label, snippet] of bad) expect({ label, found: auditSource(snippet).length > 0 }).toEqual({ label, found: true });
		expect(auditSource("childProcess.execFile('x', [], {}, guarded((e) => {}));")).toEqual([]);
	});

	test("only node:child_process, node:fs, node:path and omp's types are imported", () => {
		const imports = [...original.matchAll(/from "([^"]+)"/g)].map(m => m[1]);
		expect(imports.sort()).toEqual(["@oh-my-pi/pi-coding-agent", "node:child_process", "node:fs", "node:path"]);
		expect([...original.matchAll(/\bimport\s*\(/g)]).toEqual([]);
		expect([...original.matchAll(/\brequire\s*\(/g)]).toEqual([]);
	});

	test("no shell, no eval, no network, no writes, no timers of its own", () => {
		expect(stripped).not.toMatch(/\bexecSync\b|\bspawnSync\b|\bspawn\b|\bchildProcess\s*\.\s*exec\s*\(|shell\s*:|\beval\b|new Function|Bun\.\$|Bun\.spawn/);
		expect(stripped).not.toMatch(/\bfetch\s*\(|node:net|node:http|node:dns|WebSocket|XMLHttpRequest/);
		expect(stripped).not.toMatch(/writeFile|appendFile|mkdir|unlink|rmSync|chmod|rename|createWriteStream/);
		expect(stripped).not.toMatch(/\bsetInterval\b|\bsetTimeout\b/);
	});

	test("the only process it starts is the command line tool, by execFile, with a deadline and a termination signal", () => {
		const starts = [...stripped.matchAll(/childProcess\s*\.\s*(\w+)\s*\(/g)].map(m => m[1]);
		expect(starts).toEqual(["execFile"]);
		expect(original).toMatch(/timeout: timeoutMs/);
		expect(original).toMatch(/killSignal: "SIGTERM"/);
		expect(original).toMatch(/maxBuffer: MAX_OUTPUT_BYTES/);
	});

	test("the extension registers no event handlers and no tools", () => {
		expect(stripped).not.toMatch(/pi\s*\.\s*on\s*\(|registerTool|registerShortcut|registerFlag|registerProvider/);
	});
});
