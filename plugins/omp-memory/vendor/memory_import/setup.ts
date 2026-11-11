/**
 * `setup`: install the Mnemopi library at omp's exact version, once, on the user's explicit request. This is the only
 * command of the importer that uses the network (besides the embedding model download it triggers, see below).
 *
 *  1. Reads omp's version (`omp --version`, or `--omp-version`).
 *  2. If a matching install already exists (omp's plugin directory, or an earlier setup), nothing is fetched.
 *  3. Prints what it will fetch and where it will put it, then asks the registry whether exactly that version of
 *     `@oh-my-pi/pi-mnemopi` exists, and refuses (installing nothing) when it does not.
 *  4. `bun add --exact @oh-my-pi/pi-mnemopi@<version>` into `<cache>/lib/<version>` (the package pins its own
 *     dependencies exactly), checks the result, and writes a completion marker.
 *  5. Checks the SQL against the omp binary and runs the behavioural self-test (selftest.ts). When either fails, what this
 *     run installed is removed again and the command exits 1: setup never leaves a library behind that every write refuses.
 *  6. Unless `--no-embeddings`, embeds one sentence in the worker process the importer uses, so that the local
 *     embedding runtime (fastembed + onnxruntime) and the embedding model are fetched now and not in the middle of an
 *     import (they go to omp's own cache directory, the same one omp uses), then repeats the self-test with that real
 *     model. A failed model download alone keeps the library (it is complete); run setup again to retry.
 */
import { randomBytes } from "node:crypto";
import { existsSync, lstatSync, mkdirSync, readdirSync, readFileSync, rmdirSync, rmSync, unlinkSync, writeFileSync } from "node:fs";
import path from "node:path";
import { createWorkerEmbedder, type Embedder } from "./embedder";
import {
	cacheLibraryRoot,
	memoryCacheDir,
	DEFAULT_EMBEDDING_MODEL,
	inspectLibrary,
	installProblems,
	LIBRARY_PACKAGE,
	type Library,
	loadLibrary,
	packageDirOf,
	readOmpVersion,
	resolveLibrary,
	SETUP_MARKER,
} from "./lib";
import { Refusal, UsageError } from "./model";
import { ensureSelfTest, selfTestRefusal } from "./selftest";
import { guarded, offProcessEvent, onProcessEvent } from "./sink";

export const DEFAULT_REGISTRY = "https://registry.npmjs.org";
const EMBED_TIMEOUT_MS = 15 * 60 * 1000;

export interface SetupOptions {
	ompBin: string;
	/** Install for this version instead of the one `omp --version` prints (omp not on PATH). */
	ompVersion?: string;
	/** The install root to use (default `<cache>/lib/<omp version>`). */
	libDir?: string;
	registry?: string;
	dryRun: boolean;
	embeddings: boolean;
	embeddingModel?: string;
	/** Makes the embedder that embeds the one sentence (default: a worker process running the library's own embedding). Tests inject one. */
	embedderFactory?: (pkgDir: string, model: string) => Embedder;
	say(line: string): void;
}

export interface SetupReport {
	ok: boolean;
	ompVersion: string;
	action: "dry-run" | "already-available" | "installed" | "removed";
	library: { kind: string; path: string; version: string } | null;
	embeddings: string;
	/** The behavioural self-test: "not run", "passed (...)" or the failed step. */
	selfTest: string;
	problems: string[];
}

/** The registry as it may be printed and recorded: no credentials, query or fragment (those are refused, and never echoed). */
function registryUrl(value: string | undefined): string {
	let url: URL;
	try {
		url = new URL(value ?? DEFAULT_REGISTRY);
	} catch {
		throw new UsageError("the registry is not a valid URL (give an https URL, or http on localhost)");
	}
	if (url.username !== "" || url.password !== "") {
		throw new UsageError("the registry URL must not contain a user name or password; keep credentials in npm's own configuration, not on this command line or in the environment");
	}
	if (url.search !== "" || url.hash !== "") throw new UsageError("the registry URL must not carry a query string or a fragment");
	const loopback = url.hostname === "127.0.0.1" || url.hostname === "localhost";
	if (url.protocol !== "https:" && !(url.protocol === "http:" && loopback)) {
		throw new UsageError(`registry ${url.origin}: https is required (http only for localhost)`);
	}
	return url.toString().replace(/\/+$/, "");
}

/** Whether the registry has exactly this version of the package. Throws a Refusal when it cannot tell. */
async function registryHasVersion(registry: string, version: string): Promise<boolean> {
	const url = `${registry}/${LIBRARY_PACKAGE.replace("/", "%2f")}/${version}`;
	let response: Response;
	try {
		response = await fetch(url, { headers: { accept: "application/json" }, signal: AbortSignal.timeout(30_000) });
	} catch (error) {
		throw new Refusal(`cannot reach the npm registry ${registry}: ${String(error).slice(0, 200)}; nothing was installed`);
	}
	if (response.status === 200) return true;
	if (response.status === 404) return false;
	throw new Refusal(`the npm registry ${registry} answered ${response.status} for ${LIBRARY_PACKAGE}@${version}; nothing was installed`);
}

async function warmEmbeddings(pkgDir: string, model: string, factory: (pkgDir: string, model: string) => Embedder): Promise<string> {
	const embedder = factory(pkgDir, model);
	const timer = setTimeout(() => embedder.close(), EMBED_TIMEOUT_MS);
	try {
		const vectors = await embedder.embed(["omp-memory setup: one sentence to load the embedding runtime"]);
		return vectors.length === 1 && (vectors[0]?.length ?? 0) > 0 ? "ready" : "failed: the embedder returned no vector";
	} catch (error) {
		return `failed: ${String(error).slice(0, 300)}`;
	} finally {
		clearTimeout(timer);
		embedder.close();
	}
}

/** A directory `setup` may replace: absent, empty, or made by an earlier setup (its package.json says so). */
function ownedBySetup(root: string): boolean {
	if (!existsSync(root) || readdirSync(root).length === 0) return true;
	try {
		const parsed = JSON.parse(readFileSync(path.join(root, "package.json"), "utf8")) as { name?: string } | null;
		return parsed?.name === "omp-memory-lib";
	} catch {
		return false;
	}
}

/** A `node` on PATH older than 18 breaks onnxruntime-node's install script (the embedding runtime): say so. */
function oldNodeHint(): string | null {
	const node = Bun.which("node");
	if (node === null) return null;
	try {
		const ran = Bun.spawnSync([node, "--version"], { stdout: "pipe", stderr: "pipe" });
		const major = Number(/^v(\d+)\./.exec(ran.stdout.toString().trim())?.[1] ?? Number.NaN);
		return major < 18 ? `the node on PATH (${node}) is ${ran.stdout.toString().trim()}, too old for onnxruntime-node's install script: put a newer node first on PATH (or remove it from PATH) and run setup again` : null;
	} catch {
		return null;
	}
}

/** Where the library installs the fastembed runtime for the library at install root `root` (omp's cache directory); null when it cannot be located. */
export async function fastembedRuntimeDir(root: string): Promise<string | null> {
	try {
		// the package is chosen at run time, like the library itself
		const utils = await import(path.join(root, "node_modules", "@oh-my-pi", "pi-utils", "src", "index.ts"));
		const runtime = await import(path.join(packageDirOf(root), "src", "core", "fastembed-runtime.ts"));
		return path.join(utils.getFastembedRuntimeDir(), runtime.fastembedRuntimeInstallPlan().versionKey);
	} catch {
		return null;
	}
}

/** `kind:inode:size:mtime` of a path (the path itself, not what a link points to), or null when it is not there. */
function fingerprintOf(file: string): string | null {
	try {
		const stat = lstatSync(file);
		return `${stat.isDirectory() ? "d" : "f"}:${stat.ino}:${stat.size}:${Math.trunc(stat.mtimeMs)}`;
	} catch {
		return null;
	}
}

const inodeOf = (fingerprint: string | null | undefined): string | undefined => fingerprint?.split(":")[1];

/**
 * The runtime directory (and the missing directories above it) that THIS run created, with the token file that proves the
 * creation and the fingerprint (path, inode, size, modification time) of each of those paths as they were when the claim was
 * complete. The directory belongs to omp and is shared with it: it is only claimed by creating it, one level at a time without
 * `mkdir -p`, so that a directory another process made first (`EEXIST`) is never taken for ours.
 */
export interface RuntimeClaim {
	dir: string;
	/** Directories this run made, outermost first. */
	created: string[];
	tokenFile: string;
	recorded: Map<string, string>;
}

/** Creates `dir` and its missing parents with plain `mkdir`; null (nothing left behind) when `dir` is there already, or when another process makes any of them first. */
export function claimRuntimeDir(dir: string): RuntimeClaim | null {
	const missing: string[] = [];
	for (let at = dir; !existsSync(at); at = path.dirname(at)) missing.unshift(at);
	if (missing.length === 0) return null;
	const created: string[] = [];
	const undo = (): void => {
		for (const made of created.reverse()) {
			try {
				rmdirSync(made); // only an empty directory: never anything another process put there
			} catch {
				// not empty, or gone
			}
		}
	};
	try {
		for (const next of missing) {
			mkdirSync(next);
			created.push(next);
		}
		const tokenFile = path.join(dir, `.omp-memory-owner-${randomBytes(12).toString("hex")}`);
		writeFileSync(tokenFile, "", { flag: "wx", mode: 0o600 });
		const recorded = new Map<string, string>();
		for (const owned of [tokenFile, ...created]) recorded.set(owned, fingerprintOf(owned) ?? "");
		return { dir, created, tokenFile, recorded };
	} catch {
		undo();
		return null;
	}
}

/**
 * Ends a claim. This run removes only what it made itself and can still recognise: its token file, and (after a failure) the
 * directories it created, each only when it is still empty. Nothing else is ever removed, whatever the library's installer or
 * another process left in the runtime directory, even a half-finished install: that stays, and the message says how to remove it
 * by hand. Each path is compared with its recorded fingerprint (path, inode, size, modification time) before it is touched, and
 * a path that differs is left alone. After a success only the token goes and the directories stay: they hold the runtime.
 *
 * Residual, not closable from here: between the check and the `unlink`/`rmdir` call another process can still act on the same
 * path. The token is a random name this run made, and a directory is removed by `rmdir`, which refuses a directory that is not
 * empty, so the exposure is an empty directory another process was about to fill.
 */
export function releaseRuntimeClaim(claim: RuntimeClaim, failed: boolean): string {
	const unchanged = (owned: string): boolean => fingerprintOf(owned) === claim.recorded.get(owned);
	const intact = new Set(claim.created.filter(unchanged)); // checked before our own token removal changes the directory
	if (unchanged(claim.tokenFile)) unlinkSync(claim.tokenFile);
	if (!failed) return "";
	for (const made of [...claim.created].reverse()) {
		const sameDirectory = intact.has(made) && inodeOf(fingerprintOf(made)) === inodeOf(claim.recorded.get(made));
		let entries: number;
		try {
			entries = readdirSync(made).length;
		} catch {
			return `the embedding runtime ${claim.dir} could not be looked at, so it was left alone (omp uses it too). If it is damaged, remove it yourself and run setup again`;
		}
		if (!sameDirectory || entries > 0) {
			if (made !== claim.dir) return `removed the empty embedding runtime directory ${claim.dir} that this run created; run setup again to retry`;
			const why = entries > 0 ? `holds ${entries} entr${entries === 1 ? "y" : "ies"} this run did not make` : "changed after this run created it";
			return `the embedding runtime ${claim.dir} ${why}, so everything in it was left in place (omp uses it too). If the runtime is damaged, remove that directory yourself and run setup again`;
		}
		try {
			rmdirSync(made);
		} catch {
			return `the embedding runtime ${claim.dir} was left in place (omp uses it too). If it is damaged, remove that directory yourself and run setup again`;
		}
	}
	return `removed the empty embedding runtime directory ${claim.dir} that this run created; run setup again to retry`;
}

export async function runSetup(options: SetupOptions): Promise<SetupReport> {
	const say = options.say;
	const ompVersion = options.ompVersion ?? readOmpVersion(options.ompBin);
	if (ompVersion === null || !/^\d+\.\d+\.\d+$/.test(ompVersion)) {
		throw new UsageError(`could not read an omp version from "${options.ompBin} --version"; put omp on PATH, pass --omp-bin, or pass --omp-version X.Y.Z`);
	}
	const model = options.embeddingModel ?? DEFAULT_EMBEDDING_MODEL;
	const report: SetupReport = { ok: false, ompVersion, action: "already-available", library: null, embeddings: "skipped", selfTest: "not run", problems: [] };
	say(`omp-memory setup: omp ${ompVersion}`);

	const registry = registryUrl(options.registry);
	const existing = resolveLibrary(ompVersion, { libDir: options.libDir });
	let root = existing.location?.root ?? options.libDir ?? cacheLibraryRoot(ompVersion);
	if (existing.location === null) {
		say(
			`will fetch from ${registry}: ${LIBRARY_PACKAGE}@${ompVersion} and its exactly pinned dependencies (pi-ai, pi-utils, pi-catalog, pi-natives and its binary for this platform; about 130 MB)`,
		);
		say(`into ${root}; bun add also keeps the packages it downloads in Bun's own install cache (${process.env.BUN_INSTALL_CACHE_DIR ?? "~/.bun/install/cache"}), and the self-test result is cached under ${memoryCacheDir()}`);
		if (options.embeddings) {
			say(`then it embeds one sentence, which downloads the local embedding runtime (fastembed, onnxruntime) and the model ${model} into omp's cache directory unless they are already there (about 450 MB)`);
		}
		if (options.dryRun) {
			say("dry run: nothing was fetched or written");
			return { ...report, ok: true, action: "dry-run" };
		}
		if (!ownedBySetup(root)) {
			throw new Refusal(`${root} exists and was not made by omp-memory setup; nothing was installed or removed. Use another --lib-dir, or an empty directory`);
		}
		if (!(await registryHasVersion(registry, ompVersion))) {
			throw new Refusal(
				`${LIBRARY_PACKAGE}@${ompVersion} does not exist on ${registry}, so there is no library that matches omp ${ompVersion}; nothing was installed. The importer needs exactly omp's version: use a released omp, or run with --omp-version of a published one only if that is the omp you run`,
			);
		}
		rmSync(root, { recursive: true, force: true }); // an interrupted earlier install has no completion marker
		mkdirSync(root, { recursive: true, mode: 0o700 });
		writeFileSync(path.join(root, "package.json"), `${JSON.stringify({ name: "omp-memory-lib", private: true }, null, "\t")}\n`, { mode: 0o600 });
		const env: Record<string, string | undefined> = { ...process.env, npm_config_registry: registry };
		delete env.BUN_BE_BUN;
		const child = Bun.spawn([process.execPath, "add", "--exact", `--registry=${registry}`, `${LIBRARY_PACKAGE}@${ompVersion}`], { cwd: root, env, stdout: "pipe", stderr: "pipe" });
		// An interrupt during the download removes what is half there, like any other failure.
		const interrupted = guarded(
			"install cleanup on a signal",
			() => {
				child.kill();
				rmSync(root, { recursive: true, force: true });
			},
			130,
		);
		onProcessEvent("SIGINT", interrupted, true);
		onProcessEvent("SIGTERM", interrupted, true);
		const [out, err, code] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited]);
		offProcessEvent("SIGINT", interrupted);
		offProcessEvent("SIGTERM", interrupted);
		for (const line of `${out}${err}`.split("\n")) if (line.trim() !== "") say(`  bun: ${line.trim()}`);
		const problems = code === 0 ? installProblems(root, ompVersion).problems : [`bun add exited ${code}`];
		if (problems.length > 0) {
			rmSync(root, { recursive: true, force: true });
			throw new Refusal(`installing ${LIBRARY_PACKAGE}@${ompVersion} failed (${problems.join("; ")}); nothing was left behind`);
		}
		writeFileSync(
			path.join(root, SETUP_MARKER),
			`${JSON.stringify({ version: ompVersion, installedAt: new Date().toISOString(), registry, bun: Bun.version }, null, "\t")}\n`,
			{ mode: 0o600 },
		);
		report.action = "installed";
		say(`installed ${LIBRARY_PACKAGE}@${ompVersion} into ${root}`);
	} else {
		root = existing.location.root;
		say(`nothing to fetch: ${LIBRARY_PACKAGE}@${ompVersion} is already available at ${existing.location.pkgDir}`);
	}
	report.library = { kind: existing.location?.kind ?? (options.libDir ? "explicit" : "cache"), path: packageDirOf(root), version: ompVersion };
	if (options.dryRun) {
		const next = options.embeddings ? "repeat the self-test and prepare the embedding runtime" : "repeat the self-test";
		say(`dry run: the library is already installed; nothing was run, fetched or written (a real run would ${next})`);
		return { ...report, ok: true, action: "dry-run" };
	}

	// What this run installed is removed again when the library turns out to be unusable: setup never leaves a library behind
	// that every write command would refuse. (A library that was already there is only reported on.)
	const installedNow = report.action === "installed";
	const abandon = (why: string): SetupReport => {
		report.problems.push(why);
		if (installedNow) {
			rmSync(root, { recursive: true, force: true });
			report.library = null;
			report.action = "removed";
			say(`removed ${root}: the library was not left installed`);
		}
		report.ok = false;
		say(`setup failed: ${report.problems.join("; ")}`);
		return report;
	};

	// Load it, check the SQL against the omp binary (only when that omp is the one the version came from), then run the
	// behavioural self-test: the importer's write sequence through the library in a scratch bank.
	const sameOmp = options.ompVersion === undefined || readOmpVersion(options.ompBin) === ompVersion;
	let lib: Library | null = null;
	if (sameOmp) {
		const inspected = await inspectLibrary({ libDir: root, ompBin: options.ompBin });
		if (!inspected.check.ok) return abandon(inspected.check.reasons.join("; "));
		lib = inspected.lib;
	} else {
		say(`omp at ${options.ompBin} is not version ${ompVersion}: the library was not checked against it`);
		const location = resolveLibrary(ompVersion, { libDir: root }).location;
		lib = location === null ? null : await loadLibrary(location);
	}
	if (lib === null) return abandon("the installed library could not be loaded");
	say("running the library self-test (the importer's write sequence in a scratch bank)...");
	const stub = (await ensureSelfTest(lib, { reuse: false })).result;
	report.selfTest = stub.ok ? `passed (${stub.ms} ms, stand-in embedder)` : `failed at step "${stub.failedStep}"`;
	if (!stub.ok) return abandon(selfTestRefusal(stub, ompVersion).message);
	if (options.embeddings) {
		say("embedding one sentence to prepare the local embedding runtime...");
		const runtimeDir = await fastembedRuntimeDir(root);
		const claim = runtimeDir === null ? null : claimRuntimeDir(runtimeDir);
		report.embeddings = await warmEmbeddings(packageDirOf(root), model, options.embedderFactory ?? createWorkerEmbedder);
		const failed = report.embeddings !== "ready";
		const said = claim === null ? "" : releaseRuntimeClaim(claim, failed);
		if (failed) {
			report.problems.push(`embedding: ${report.embeddings}`);
			const hint = oldNodeHint();
			if (hint !== null) report.problems.push(hint);
			if (claim !== null) say(said);
			else if (runtimeDir === null) say("the embedding runtime directory could not be located; nothing was removed");
			else say(`the embedding runtime ${runtimeDir} was not made by this run (it was there before, or another process made it), so it was left alone; omp uses it too. The failure may be the model download or the network; if the runtime itself is damaged, remove that directory yourself and run setup again`);
		} else {
			say("repeating the self-test with the real embedding model...");
			const real = (await ensureSelfTest(lib, { reuse: false, kind: "real", model })).result;
			report.selfTest = real.ok ? `passed (${stub.ms} ms with a stand-in embedder, ${real.ms} ms with ${model})` : `failed at step "${real.failedStep}" with ${model}`;
			if (!real.ok) return abandon(selfTestRefusal(real, ompVersion).message);
		}
	}
	report.ok = report.problems.length === 0;
	say(report.ok ? "setup complete" : `setup finished with problems: ${report.problems.join("; ")}`);
	return report;
}
