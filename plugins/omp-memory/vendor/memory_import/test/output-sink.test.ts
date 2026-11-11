/**
 * Source-tree lint: every byte the importer prints or writes goes through sink.ts (and outputs.ts for report files). This reads
 * every source file of the importer (not the tests, not the sink itself) and fails on a direct console call, a process stream
 * write, a file write, a child process whose stdio is inherited or whose stderr is not captured, and a `throw new Error(...)`
 * whose argument is anything but a plain string (use `fail(category, ...labels)` from the sink, which scrubs the labels).
 *
 * The allow-list is narrow on purpose: each entry names a file, a rule and a fragment of the line, and says why that line cannot
 * carry text from a source. An entry that matches nothing fails the test, so the list cannot go stale.
 */
import { describe, expect, test } from "bun:test";
import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { useLongTimeouts } from "./timeout";

useLongTimeouts();

const ROOT = path.resolve(import.meta.dir, "..");

interface Rule {
	name: string;
	/** The lines (or, for `statement` rules, the statements) that break the rule. */
	test(text: string): boolean;
	statements?: boolean;
}

const RULES: Rule[] = [
	// any use of the `console` object (every method and property, a destructured or aliased use, iterating it), not a fixed list
	{ name: "console", test: text => /\bconsole\b/.test(text) },
	{ name: "process stream", test: text => /\bprocess\.(stdout|stderr)\b/.test(text) },
	{
		name: "file write",
		test: text =>
			!/^\s*import\b/.test(text) &&
			/\b(writeFile|writeFileSync|writeSync|appendFile|appendFileSync|createWriteStream|copyFile|copyFileSync|cp|cpSync|rename|renameSync|truncate|truncateSync|ftruncateSync|linkSync|symlinkSync|Bun\.write)\s*\(/.test(text),
	},
	// `.write(` on a stream or a pipe: terminal streams, a child's stdin, a socket
	{ name: "stream write", test: text => /\.\s*write\s*\(/.test(text) },
	{ name: "process event registration", test: text => /\bprocess\.(on|once|off|addListener|prependListener|removeListener)\s*\(/.test(text) },
	{ name: "promise rejection path", test: text => /\.catch\s*\(|\bnew Promise\b|\breject\s*\(/.test(text) },
	{ name: "inherited stdio", test: text => /["']inherit["']/.test(text) },
	{
		name: "child process without captured stderr",
		statements: true,
		test: text => /\b(Bun\.spawn|Bun\.spawnSync|spawnSync|execSync)\s*\(/.test(text) && !/stderr:\s*["'](pipe|ignore)["']/.test(text),
	},
	{
		name: "throw new Error with a non-literal argument",
		test: text => {
			const match = /throw new Error\((.*)$/.exec(text);
			if (match === null) return false;
			const argument = (match[1] ?? "").replace(/\);?\s*$/, "").trim();
			return !/^"([^"\\]|\\.)*"$/.test(argument) && !/^'([^'\\]|\\.)*'$/.test(argument);
		},
	},
];

interface Allowed {
	file: string;
	rule: string;
	/** A fragment of the line (or statement) it covers. */
	match: string;
	why: string;
}

const ALLOWED: Allowed[] = [
	{ file: "model.ts", rule: "file write", match: "writeFileSync(fd,", why: "the atomic JSON writer: report files reach it through writeOutput (scrubbed); the ledger and the self-test cache hold no source text beyond keys that passed the provenance hold" },
	{ file: "ledger.ts", rule: "file write", match: "writeSync(fd, record)", why: "the lock file's own record: a pid, a random token and a time" },
	{ file: "setup.ts", rule: "file write", match: "writeFileSync(tokenFile", why: "an empty ownership token file with a random name" },
	{ file: "setup.ts", rule: "file write", match: 'writeFileSync(path.join(root, "package.json")', why: "the install manifest {name, private}, nothing from a source" },
	{ file: "setup.ts", rule: "file write", match: "writeFileSync(", why: "the install marker: version, time, registry URL without credentials (refused when it has any), Bun's version" },
	{ file: "embed_worker.ts", rule: "process stream", match: "process.stdout.write(", why: "the worker's reply to its parent process over a pipe (vectors, or an error string the parent shows only through describeFailure); not the terminal" },
	{ file: "embed_worker.ts", rule: "throw new Error with a non-literal argument", match: "returned no vectors", why: "names the embedding model (a flag value), shown only through describeFailure in the parent" },
	{ file: "embedder.ts", rule: "throw new Error with a non-literal argument", match: "reply.error", why: "the worker's own error text, shown only through describeFailure" },
	{ file: "banks.ts", rule: "throw new Error with a non-literal argument", match: "library inserted", why: "the message holds two row counts and nothing else" },
	{ file: "cli.ts", rule: "stream write", match: "process.stderr.write(", why: "the entry's last-resort line, written before any other module is loaded (the sink may be what failed to load); its message comes from the sink when it loads and is withheld when it does not" },
	{ file: "cli.ts", rule: "process stream", match: "process.stderr.write(", why: "the entry's last-resort line, written before any other module is loaded; see the entry's header" },
	{ file: "cli.ts", rule: "process event registration", match: "process.on(", why: "the last-resort handlers for an uncaught exception and an unhandled rejection, installed before the application loads; each prints only a categorical line" },
	{ file: "embed_worker.ts", rule: "console", match: "for await (const line of console)", why: "Bun's way to read the worker's own stdin line by line; nothing is printed through it" },
	{ file: "embed_worker.ts", rule: "stream write", match: "process.stdout.write(", why: "the worker's reply to its parent over a pipe; see the process stream entry" },
	{ file: "embedder.ts", rule: "stream write", match: "child.stdin.write(", why: "the request to the embedding worker over its stdin pipe: texts the importer has already redacted" },
	{ file: "redact.ts", rule: "stream write", match: "child.stdin.write(", why: "the text sent to the redaction bridge over its stdin pipe: that is the bridge's job" },
	{ file: "ledger.ts", rule: "file write", match: "linkSync(", why: "takes and moves the importer's lock file by hard link: the file's content is a pid, a token and a time" },
	{ file: "ledger.ts", rule: "file write", match: "renameSync(", why: "moves a stale lock file aside: the same content" },
	{ file: "model.ts", rule: "file write", match: "renameSync(temp, file)", why: "the last step of the atomic JSON writer (see the writeFileSync entry above)" },
	{ file: "verify_versions.ts", rule: "console", match: "console", why: "a developer script that prints a table of published library versions and test results; it reads no memory data and is not a command of the importer" },
	{ file: "verify_versions.ts", rule: "file write", match: "writeFileSync(", why: "same script: its own scratch install and a JSON table of version results" },
];

function sourceFiles(): string[] {
	const names = readdirSync(ROOT).filter(name => name.endsWith(".ts") && name !== "sink.ts");
	const sources = readdirSync(path.join(ROOT, "sources")).filter(name => name.endsWith(".ts"));
	return [...names, ...sources.map(name => `sources/${name}`)];
}

/** Comments removed (a mention of `console.log` in a comment is not a call), keeping line numbers. */
function code(text: string): string[] {
	return text
		.replace(/\/\*[\s\S]*?\*\//g, block => block.replace(/[^\n]/g, " "))
		.split("\n")
		.map(line => line.replace(/^\s*\/\/.*$/, "").replace(/\s\/\/ .*$/, ""));
}

interface Finding {
	file: string;
	line: number;
	rule: string;
	text: string;
}

function findings(): Finding[] {
	const out: Finding[] = [];
	for (const file of sourceFiles()) {
		const lines = code(readFileSync(path.join(ROOT, file), "utf8"));
		for (const rule of RULES) {
			if (rule.statements) {
				const text = lines.join("\n");
				for (const match of text.matchAll(/\b(Bun\.spawnSync|Bun\.spawn|spawnSync|execSync)\s*\(/g)) {
					const start = match.index ?? 0;
					const statement = text.slice(start, text.indexOf(";", start) + 1);
					if (rule.test(statement)) out.push({ file, line: text.slice(0, start).split("\n").length, rule: rule.name, text: statement.slice(0, 160).replace(/\s+/g, " ") });
				}
				continue;
			}
			for (const [index, line] of lines.entries()) if (rule.test(line)) out.push({ file, line: index + 1, rule: rule.name, text: line.trim() });
		}
	}
	return out;
}

const covered = (finding: Finding): Allowed | undefined =>
	ALLOWED.find(entry => entry.file === finding.file && entry.rule === finding.rule && finding.text.includes(entry.match));

describe("one output sink", () => {
	test("no source file prints, writes or throws around the sink, except the allow-listed lines", () => {
		const loose = findings().filter(finding => covered(finding) === undefined);
		expect(loose.map(finding => `${finding.file}:${finding.line} [${finding.rule}] ${finding.text}`)).toEqual([]);
	});

	test("every allow-list entry still matches a line (no stale entries) and says why", () => {
		const all = findings();
		for (const entry of ALLOWED) {
			expect(entry.why.length).toBeGreaterThan(20);
			expect({ entry: `${entry.file} ${entry.rule} ${entry.match}`, used: all.some(finding => covered(finding) === entry) }).toEqual({ entry: `${entry.file} ${entry.rule} ${entry.match}`, used: true });
		}
	});

	test("the entry loads nothing before its handlers exist: its only static import is a type import", () => {
		const imports = readFileSync(path.join(ROOT, "cli.ts"), "utf8")
			.split("\n")
			.filter(line => /^import\b/.test(line));
		expect(imports.filter(line => !/^import type\b/.test(line))).toEqual([]);
	});

	test("the lint itself sees what it should: a throw with a variable, a console call and a spawn without stderr", () => {
		const samples: [string, string, boolean][] = [
			["throw new Error", "throw new Error(`bank ${bank} is odd`);", true],
			["throw new Error", 'throw new Error("a plain message");', false],
			["throw new Error", "throw new Error(message);", true],
			["console", "console.error(x);", true],
			["process stream", "process.stdout.write(x);", true],
			["file write", "writeFileSync(file, text);", true],
			["file write", "renameSync(temp, file);", true],
			["file write", "copyFileSync(a, b);", true],
			["console", "const { log } = console;", true],
			["console", "for await (const line of console) {", true],
			["console", "console.table(rows);", true],
			["stream write", "child.stdin.write(text);", true],
			["process event", "process.on('exit', cleanup);", true],
			["promise rejection", "run().catch(error => print(error));", true],
			["promise rejection", "new Promise((resolve, reject) => reject(error));", true],
		];
		for (const [name, text, expected] of samples) {
			const rule = RULES.find(item => item.name.startsWith(name.split(" ").slice(0, 2).join(" ")));
			expect({ text, flagged: rule?.test(text) }).toEqual({ text, flagged: expected });
		}
		const spawn = RULES.find(item => item.name.startsWith("child process"));
		expect(spawn?.test('Bun.spawn(["x"], { stdout: "pipe" });')).toBe(true);
		expect(spawn?.test('Bun.spawn(["x"], { stdout: "pipe", stderr: "pipe" });')).toBe(false);
	});
});
