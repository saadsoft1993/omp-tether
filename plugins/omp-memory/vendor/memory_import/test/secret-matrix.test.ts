/**
 * Black-box: a recognised secret placed in every position a source or a flag can carry one, through the real command line
 * (`bun cli.ts <command>` in a scratch home, real library banks), must not appear in anything the importer prints or writes:
 * stdout, stderr, the failure message, the report, samples, project-map and ledger files.
 *
 * Positions: a folder name, a file name, the name of an existing bank (also after it carries a run, for verify and undo), a
 * Hindsight project key, tag and document id (with a valid and an invalid project map), a path flag naming a file whose own name
 * is the secret (an unreadable export, an invalid ledger, an invalid map), a bad flag value, an unknown flag, a path that
 * only exists in the message of a failure, and `setup --dry-run` with a lib dir named like the secret.
 *
 * Stand-ins, because they cannot be driven here: `import --apply` runs in this process through `runImport` with a fake
 * embedder (the command line would need the real embedding model and so the network); its report FILE is checked, and the rows it
 * writes are then verified and undone through the command line. `setup` runs with `--dry-run` only (no network).
 */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { mkdirSync, readdirSync, readFileSync, renameSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { runImport } from "../engine";
import { encodeProjectName } from "../folders";
import { type Env, makeEnv } from "./helpers";
import { useLongTimeouts } from "./timeout";

useLongTimeouts();

// built at run time so that no file of this repository holds a literal token
const TOKEN = `ghp_${"Zk9Qm2Xv7Lp4Rt8NwB3cD5eF6gH1jK0aS7uY"}`;

interface Shape {
	name: string;
	/** The secret as a name carries it. */
	expr: string;
	/** What must never appear, in any case. */
	core: string;
}

const SHAPES: Shape[] = [
	{ name: "a PIN assignment", expr: "password=902817", core: "902817" },
	{ name: "a token with a known prefix", expr: TOKEN, core: TOKEN },
	{ name: "a percent-encoded assignment", expr: "password%3D902817", core: "902817" },
	{ name: "a case-changed assignment", expr: "PASSWORD=Kj83Hq72", core: "Kj83Hq72" },
];

const CLI = path.resolve(import.meta.dir, "../cli.ts");

interface Ran {
	label: string;
	code: number | null;
	text: string;
}

describe("a secret in any position never reaches the output (through the command line)", () => {
	for (const shape of SHAPES) {
		describe(shape.name, () => {
			let env: Env;
			const ran: Ran[] = [];
			const files: string[] = [];
			const sanitised = shape.expr.replace(/[^a-zA-Z0-9_-]+/g, "-");

			const run = (label: string, args: string[], extraEnv: Record<string, string> = {}): Ran => {
				const child = Bun.spawnSync([process.execPath, CLI, ...args], { env: { ...process.env, HOME: env.home, ...extraEnv }, timeout: 120_000 });
				const result = { label, code: child.exitCode, text: `${child.stdout.toString()}\n${child.stderr.toString()}` };
				ran.push(result);
				return result;
			};

			const common = (extra: string[] = []): string[] => [
				"--banks-dir", env.banksDir,
				"--ledger", env.ledger,
				"--claude-home", env.home,
				"--lib-dir", env.libDir,
				"--omp-bin", env.fakeOmp,
				"--force-closed",
				...extra,
			];

			/** A project folder under home with its Claude project directory and one memory file. */
			const note = (folderName: string, file: string, body: string): string => {
				const folder = path.join(env.home, "work", folderName);
				mkdirSync(folder, { recursive: true });
				const dir = path.join(env.home, ".claude", "projects", encodeProjectName(folder));
				mkdirSync(path.join(dir, "memory"), { recursive: true });
				writeFileSync(path.join(dir, "11111111-1111-4111-8111-111111111111.jsonl"), `${JSON.stringify({ type: "user", cwd: folder })}\n`);
				writeFileSync(path.join(dir, "memory", file), `---\nname: A note\n---\n\n${body}\n`);
				return folder;
			};

			beforeAll(async () => {
				env = await makeEnv();
				note(shape.expr, "in-secret-folder.md", "A note that lives in a folder named like the secret.");
				note("plain-b", `${shape.expr}.md`, "A note whose file name is the secret.");
				note("plain-b", "ok.md", "An ordinary note in the plain folder.");
				note("ordinary", "fine.md", "An ordinary note that is imported, verified and undone.");
				env.emptyBank(`${sanitised}-extra`); // an existing bank that is named like the secret
				const hindsightFact = (id: string, tags: string[], documentId: string) => ({ id, text: `A fact about ${id}.`, fact_type: "world", state: "valid", tags, document_id: documentId, date: "2026-10-01T10:00:00Z" });
				writeFileSync(
					path.join(env.root, "export.json"),
					JSON.stringify({
						banks: {
							one: [
								hindsightFact("h1", [`project:${shape.expr}`], "doc-1"),
								hindsightFact("h2", ["project:plain-b", shape.expr], "doc-2"),
								hindsightFact("h3", ["project:plain-b"], shape.expr),
								hindsightFact("h4", ["project:plain-b"], "doc-4"),
							],
						},
					}),
				);
				const folder = path.join(env.home, "work", "plain-b");
				writeFileSync(path.join(env.root, "map-ok.json"), JSON.stringify({ projects: { [shape.expr.toLowerCase()]: folder, "plain-b": folder } }));
				writeFileSync(path.join(env.root, "map-bad.json"), JSON.stringify({ projects: { [shape.expr.toLowerCase()]: 5 } }));
				writeFileSync(path.join(env.root, `${shape.expr}-map.json`), JSON.stringify({ projects: { x: 5 } }));
				writeFileSync(path.join(env.root, `${shape.expr}-export.json`), "{ not json");
				writeFileSync(path.join(env.root, `${shape.expr}-ledger.json`), '{"version": 2}');
			});
			afterAll(() => env.cleanup());

			test("scan, dry-run, hindsight, project-map, library, setup --dry-run, doctor and the failure paths", () => {
				const out = (name: string): string => {
					const file = path.join(env.root, name);
					files.push(file, file.replace(/\.json$/, ".samples.json"));
					return file;
				};
				run("scan", ["scan", ...common(["--out", out("scan.json")])]);
				run("dry-run", ["dry-run", ...common(["--out", out("plan.json")])]);
				run("dry-run, an out file named like the secret", ["dry-run", ...common(["--out", out(`${shape.expr}.json`)])]);
				run("dry-run, hindsight, valid map", ["dry-run", ...common(["--source", "hindsight", "--hindsight-export", path.join(env.root, "export.json"), "--project-map", path.join(env.root, "map-ok.json"), "--out", out("hs.json")])]);
				run("dry-run, hindsight, invalid map", ["dry-run", ...common(["--source", "hindsight", "--hindsight-export", path.join(env.root, "export.json"), "--project-map", path.join(env.root, "map-bad.json")])]);
				run("dry-run, an invalid map named like the secret", ["dry-run", ...common(["--source", "hindsight", "--hindsight-export", path.join(env.root, "export.json"), "--project-map", path.join(env.root, `${shape.expr}-map.json`)])]);
				run("dry-run, an export named like the secret", ["dry-run", ...common(["--source", "hindsight", "--hindsight-export", path.join(env.root, `${shape.expr}-export.json`)])]);
				run("dry-run, a ledger named like the secret", ["dry-run", "--banks-dir", env.banksDir, "--ledger", path.join(env.root, `${shape.expr}-ledger.json`), "--claude-home", env.home, "--lib-dir", env.libDir, "--omp-bin", env.fakeOmp]);
				run("dry-run, a flag value", ["dry-run", ...common(["--source", shape.expr])]);
				run("dry-run, an unknown bank", ["dry-run", ...common(["--banks", shape.expr])]);
				run("dry-run, an embedding model", ["dry-run", ...common(["--embedding-model", shape.expr])]);
				run("dry-run, an unknown flag", ["dry-run", ...common([`--${shape.expr}`])]);
				run("dry-run, a stray argument", ["dry-run", ...common([shape.expr])]);
				run("dry-run, a claude home named like the secret", ["dry-run", "--banks-dir", env.banksDir, "--ledger", env.ledger, "--claude-home", path.join(env.root, shape.expr), "--lib-dir", env.libDir, "--omp-bin", env.fakeOmp]);
				run("project-map", ["project-map", "--hindsight-export", path.join(env.root, "export.json"), "--claude-home", env.home, "--out", out("draft.json")]);
				run("project-map, an unreadable export", ["project-map", "--hindsight-export", path.join(env.root, `${shape.expr}-export.json`), "--claude-home", env.home]);
				run("library", ["library", "--lib-dir", path.join(env.root, shape.expr), "--omp-bin", env.fakeOmp]);
				run("setup --dry-run", ["setup", "--dry-run", "--omp-version", "9.9.9", "--lib-dir", path.join(env.root, shape.expr), "--no-embeddings"]);
				run("doctor", ["doctor", "--banks-dir", env.banksDir, "--lib-dir", env.libDir, "--omp-bin", env.fakeOmp]);
				for (const item of ran) expect({ label: item.label, ran: item.code !== null }).toEqual({ label: item.label, ran: true });
			});

			test("an import with a stand-in embedder, then verify and undo through the command line, with the bank renamed after the secret", async () => {
				const outFile = path.join(env.root, "import.json");
				files.push(outFile, outFile.replace(/\.json$/, ".samples.json"));
				const applied = await runImport(env.options({ command: "import", apply: true, runId: "imp-matrix-1", out: outFile }));
				expect(applied.counts.imported).toBeGreaterThanOrEqual(2); // the plain notes: the secret-named ones are held, not imported
				// the ledger as the import wrote it (the renamed bank below is the test's own doing, and a ledger must name the banks it records)
				ran.push({ label: "the ledger the import wrote", code: 0, text: readFileSync(env.ledger, "utf8") });
				// the bank of the ordinary folder takes a name that carries the secret, in the ledger as on disk
				const ordinaryBank = env.bankOf(path.join(env.home, "work", "ordinary"));
				const renamed = sanitised; // exactly the secret, with no hash suffix to strip
				renameSync(path.join(env.banksDir, ordinaryBank), path.join(env.banksDir, renamed));
				writeFileSync(env.ledger, readFileSync(env.ledger, "utf8").replaceAll(ordinaryBank, renamed));
				run("verify", ["verify", "--run", "imp-matrix-1", ...common()]);
				run("undo", ["undo", "--run", "imp-matrix-1", ...common()]);
				run("undo --apply", ["undo", "--run", "imp-matrix-1", "--apply", ...common(["--out", path.join(env.root, "undo.json")])]);
				files.push(path.join(env.root, "undo.json"));
				run("verify again", ["verify", "--run", "imp-matrix-1", ...common(["--out", path.join(env.root, "verify.json")])]);
				files.push(path.join(env.root, "verify.json"));
				run("doctor, with the renamed bank", ["doctor", "--banks-dir", env.banksDir, "--lib-dir", env.libDir, "--omp-bin", env.fakeOmp]);
				const verified = ran.find(item => item.label === "verify");
				expect(verified?.code).not.toBeNull();
			});

			test("no stream, message or file carries the secret, its case, its encoding or its sanitised form", () => {
				const forbidden = [...new Set([shape.core, shape.core.toLowerCase(), shape.core.toUpperCase(), shape.expr, shape.expr.toLowerCase(), encodeURIComponent(shape.expr), sanitised, sanitised.toLowerCase()])];
				const fileTexts: Ran[] = [];
				for (const file of new Set(files)) {
					try {
						if (statSync(file).isFile()) fileTexts.push({ label: `file ${path.basename(file).replaceAll(shape.expr, "<name>")}`, code: 0, text: readFileSync(file, "utf8") });
					} catch {
						// a command that failed before it wrote its file
					}
				}
				expect(ran.length).toBeGreaterThan(20);
				const leaks: string[] = [];
				for (const item of [...ran, ...fileTexts]) {
					// what a command prints may mention the PATH of a file whose name is the secret only as a label
					for (const value of forbidden) if (item.text.includes(value)) leaks.push(`${item.label}: the secret (${value.length} characters) appears`);
				}
				expect(leaks).toEqual([]);
				// and the importer made no bank or file named after a held item
				expect(readdirSync(env.banksDir).filter(name => name.includes(shape.core) && name !== sanitised && name !== `${sanitised}-extra`)).toEqual([]);
			});
		});
	}
});
