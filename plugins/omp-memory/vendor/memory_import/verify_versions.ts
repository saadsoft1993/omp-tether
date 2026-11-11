#!/usr/bin/env bun
/**
 * Maintainer tool: run the importer's library checks against published releases of `@oh-my-pi/pi-mnemopi`, one install per
 * version, and print a table: does the package meet the structural SQL requirements, does the behavioural self-test pass
 * (and which step fails if not), and, with `--suite`, does the importer's own Bun test suite pass when its scratch banks
 * are built with that version. This is how compat.json's range is established.
 *
 *   bun verify_versions.ts [--root DIR] [--suite] [--json FILE] VERSION...
 *
 * `--root` (default: a new directory under the system temp directory) holds one install root per version
 * (`<root>/<version>`); an install that is already there is reused. Installs go through `bun add --exact`; set
 * BUN_INSTALL and HOME to a scratch directory first when the Bun on this machine must not be touched.
 */
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { LIBRARY_PACKAGE, loadLibrary, PARITY_FILES, resolveLibrary, unmetRequirements } from "./lib";
import { runSelfTest, stubEmbedder } from "./selftest";

interface Row {
	version: string;
	installed: boolean;
	structural: string;
	selfTest: string;
	suite?: string;
}

const args = process.argv.slice(2);
let root = "";
let jsonFile = "";
let suite = false;
const versions: string[] = [];
for (let index = 0; index < args.length; index += 1) {
	const arg = args[index] ?? "";
	if (arg === "--root") root = args[(index += 1)] ?? "";
	else if (arg === "--json") jsonFile = args[(index += 1)] ?? "";
	else if (arg === "--suite") suite = true;
	else if (/^\d+\.\d+\.\d+$/.test(arg)) versions.push(arg);
	else {
		console.error(`unexpected argument ${arg}; usage: bun verify_versions.ts [--root DIR] [--suite] [--json FILE] VERSION...`);
		process.exit(2);
	}
}
if (versions.length === 0) {
	console.error("usage: bun verify_versions.ts [--root DIR] [--suite] [--json FILE] VERSION...");
	process.exit(2);
}
if (root === "") root = mkdtempSync(path.join(tmpdir(), "omp-memory-versions-"));

function install(version: string): string | null {
	const dir = path.join(root, version);
	if (resolveLibrary(version, { libDir: dir }).location !== null) return null;
	mkdirSync(dir, { recursive: true });
	writeFileSync(path.join(dir, "package.json"), `${JSON.stringify({ name: "omp-memory-lib", private: true })}\n`);
	const env: Record<string, string | undefined> = { ...process.env };
	delete env.BUN_BE_BUN;
	const ran = Bun.spawnSync([process.execPath, "add", "--exact", `${LIBRARY_PACKAGE}@${version}`], { cwd: dir, env, stdout: "pipe", stderr: "pipe" });
	return ran.exitCode === 0 ? null : `bun add exited ${ran.exitCode}: ${ran.stderr.toString().slice(0, 200)}`;
}

const rows: Row[] = [];
for (const version of versions) {
	const row: Row = { version, installed: false, structural: "-", selfTest: "-" };
	rows.push(row);
	const problem = install(version);
	const location = resolveLibrary(version, { libDir: path.join(root, version) }).location;
	if (problem !== null || location === null) {
		row.selfTest = problem ?? "not installed";
		continue;
	}
	row.installed = true;
	const unmet = unmetRequirements(PARITY_FILES.map(file => readFileSync(path.join(location.pkgDir, file), "utf8")).join("\n"));
	row.structural = unmet.length === 0 ? "pass" : `FAIL: ${unmet.join(", ")}`;
	const lib = await loadLibrary(location);
	const result = await runSelfTest(lib, { embedder: stubEmbedder(), kind: "stub" });
	row.selfTest = result.ok ? `pass (${result.ms} ms)` : `FAIL at ${result.failedStep}: ${result.steps.find(step => !step.ok)?.detail ?? ""}`;
	if (suite) {
		const ran = Bun.spawnSync([process.execPath, "test"], {
			cwd: import.meta.dir,
			env: { ...process.env, OMP_MEMORY_TEST_VERSION: version, OMP_MEMORY_TEST_LIB_DIR: path.join(root, version) },
			stdout: "pipe",
			stderr: "pipe",
		});
		const text = `${ran.stdout.toString()}${ran.stderr.toString()}`;
		const pass = /(\d+) pass/.exec(text)?.[1] ?? "?";
		const fail = /(\d+) fail/.exec(text)?.[1] ?? "?";
		row.suite = `${ran.exitCode === 0 ? "pass" : "FAIL"} (${pass} pass, ${fail} fail)`;
	}
}

const header = ["VERSION", "STRUCTURAL SQL", "SELF-TEST", ...(suite ? ["BUN SUITE"] : [])];
const table = rows.map(row => [row.version, row.structural, row.selfTest, ...(suite ? [row.suite ?? "-"] : [])]);
const widths = header.map((title, column) => Math.max(title.length, ...table.map(cells => (cells[column] ?? "").length)));
const line = (cells: string[]): string => cells.map((cell, column) => cell.padEnd(widths[column] ?? 0)).join("  ").trimEnd();
console.log([line(header), ...table.map(line)].join("\n"));
if (jsonFile !== "") writeFileSync(jsonFile, `${JSON.stringify(rows, null, 2)}\n`);
process.exit(rows.every(row => row.installed && row.structural === "pass" && row.selfTest.startsWith("pass") && (row.suite === undefined || row.suite.startsWith("pass"))) ? 0 : 1);
