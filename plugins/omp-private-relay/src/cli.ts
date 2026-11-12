// omp-private-relay command line: init, status, doctor, check, pair, devices, revoke, rebuild-web, dropins, run.
//
// Nothing here edits omp's settings, writes files outside this plugin's own directories, downloads anything or touches
// another tool's configuration without asking (or being given the flag that says yes). Started through `bin/omp-private-relay`,
// which runs this file on omp's embedded Bun.
import * as fs from "node:fs";
import * as net from "node:net";
import * as path from "node:path";
import * as readline from "node:readline";
import { createAuth } from "./auth";
import { type Config, ConfigError, configFilePath, DEFAULT_PORTS, loadConfig, minimalFile, readConfigFile, relayUrl } from "./config";
import { dropinPaths, healthDropin, servicesDropin, strayOverrides, writeDropin } from "./dropins";
import { renderFindings, runCheck, runDoctor } from "./doctor";
import { ensurePrivateDir, readFdBounded } from "./fsutil";
import { main as runRelay } from "./main";
import { fetchProvenStats, proveListener, readCheckKey } from "./listener-check";
import { PairingError, PairingWriter } from "./pairing-store";
import { configDir, pluginRoot } from "./paths";
import { envForBun, envForOmp, findOmp, minorOf, probeOmp } from "./runtime";
import { assetUrl, download, type Fetcher, inspectInstalled, installTree, type InstalledBuild, loadWebBuilds, MAX_ASSET_BYTES, readTree, sha256, treeHash, verifyAsset, WebAssetError, type WebBuilds } from "./web-asset";

type Env = Record<string, string | undefined>;

/** What tests replace: the pinned builds and the network fetch. Production code passes nothing. */
export interface CliDeps {
	builds?: WebBuilds;
	fetcher?: Fetcher;
	/** Replaces scripts/build-web.sh (tests). */
	buildScript?: string;
}

export interface Io {
	out(text: string): void;
	err(text: string): void;
	/** Next line typed by the user, or null at end of input. */
	line(): Promise<string | null>;
	/** False when input is not a terminal that someone is typing at (a service, a pipe that stays open): nothing is waited for. */
	interactive?: boolean;
}

interface Args {
	positional: string[];
	flags: Map<string, string | true>;
}

const VALUE_FLAGS = new Set(["adapter", "host-port", "guest-port", "asset-file", "asset-base", "omp-src", "tag", "dir"]);

export function parseArgs(argv: string[]): Args {
	const positional: string[] = [];
	const flags = new Map<string, string | true>();
	for (let i = 0; i < argv.length; i++) {
		const a = argv[i] as string;
		if (!a.startsWith("--")) {
			positional.push(a);
			continue;
		}
		const [name, inline] = a.slice(2).split(/=(.*)/s, 2) as [string, string | undefined];
		if (inline !== undefined) flags.set(name, inline);
		else if (VALUE_FLAGS.has(name)) {
			const next = argv[i + 1];
			if (next === undefined || next.startsWith("--")) throw new UsageError(`--${name} needs a value`);
			flags.set(name, next);
			i++;
		} else flags.set(name, true);
	}
	return { positional, flags };
}

class UsageError extends Error {}

const HELP = `omp-private-relay: a private Collab relay for omp, on your own machine

  init            set it up: config file, web client, and the exact omp settings to use (asks before each step)
  status          what is configured and whether the relay is running (--json)
  doctor          check everything and say how to fix what is wrong (--json)
  check           quiet yes/no: do both listeners prove to be this relay? (used by the health drop-in)
  pair            print a one-time link that pairs one browser (token and own-store setups)
  devices         list paired browsers
  revoke <id|all> unpair one browser (id from "devices") or every browser
  rebuild-web     build the web client from an omp source checkout (--omp-src DIR --tag vX.Y.Z)
  dropins         show the omp-services drop-ins for this relay (--write to write them)
  run             run the relay in the foreground

init flags: --yes (create files, download the web client) --adapter token|tailscale-serve --host-port N --guest-port N
            --asset-file FILE (use a downloaded web client) --asset-base URL --skip-web --set-omp-settings
Nothing edits omp's settings unless you pass --set-omp-settings or type y when asked.
`;

function version(): string {
	return (JSON.parse(fs.readFileSync(path.join(pluginRoot(), "package.json"), "utf8")) as { version: string }).version;
}

class Prompter {
	constructor(
		private readonly io: Io,
		private readonly yes: boolean,
	) {}

	/** Asks a yes/no question. `--yes` answers yes to questions marked `coveredByYes`; end of input answers no. */
	async confirm(question: string, coveredByYes = true): Promise<boolean> {
		if (this.yes && coveredByYes) {
			this.io.out(`${question} yes (--yes)\n`);
			return true;
		}
		this.io.out(`${question} [y/N] `);
		// --yes with no terminal behind it (a script, a service): a question that --yes does not cover is a no, not a wait.
		const answer = this.yes && this.io.interactive === false ? null : await this.io.line();
		this.io.out(answer === null ? "\n(no answer: not done)\n" : "");
		return answer !== null && /^y(es)?$/i.test(answer.trim());
	}
}

function portIsFree(port: number): Promise<boolean> {
	const { promise, resolve } = Promise.withResolvers<boolean>();
	const s = net.createServer();
	s.once("error", () => resolve(false));
	s.listen(port, "127.0.0.1", () => s.close(() => resolve(true)));
	return promise;
}

async function firstFreePort(start: number, taken: number[]): Promise<number> {
	for (let p = start; p < Math.min(start + 200, 65_535); p++) if (!taken.includes(p) && (await portIsFree(p))) return p;
	throw new Error(`no free port found from ${start}`);
}

function inDefaultConfigDir(file: string, env: Env): boolean {
	return path.dirname(file) === configDir(env);
}

function writeConfigFile(file: string, doc: Record<string, unknown>, env: Env): void {
	if (inDefaultConfigDir(file, env)) ensurePrivateDir(path.dirname(file));
	else fs.mkdirSync(path.dirname(file), { recursive: true });
	const tmp = `${file}.${process.pid}.tmp`;
	fs.rmSync(tmp, { force: true });
	fs.writeFileSync(tmp, `${JSON.stringify(doc, null, "\t")}\n`, { mode: 0o600, flag: "wx" });
	fs.renameSync(tmp, file);
}

/** The exact `omp config set` lines for this configuration. */
async function ompSettingLines(config: Config): Promise<{ lines: string[]; note?: string }> {
	const lines = [`omp config set collab.relayUrl ${relayUrl(config)}`];
	if (config.auth.adapter === "token") {
		lines.push(`omp config set collab.webUrl http://127.0.0.1:${config.listen.guestPort}`);
		return { lines };
	}
	const auth = createAuth(config);
	const run = auth.start({ log() {}, dropGuests() {} });
	await auth.settled();
	const link = auth.pairingLink("x");
	run.stop();
	const servePort = config.auth.tailscale.servePort ?? config.listen.guestPort;
	const origin = link ? link.slice(0, link.indexOf("/pair")) : `https://<this-machine>.<your-tailnet>.ts.net${servePort === 443 ? "" : `:${servePort}`}`;
	lines.push(`omp config set collab.webUrl ${origin}`);
	return link ? { lines } : { lines, note: "Tailscale did not answer, so the machine's name is a placeholder: replace it with the name `tailscale status` shows for this machine. If the tailscale command is not on PATH (and not in /usr/local/bin, /opt/homebrew/bin or the Tailscale app), set auth.tailscale.bin in the config file, or OMP_PRIVATE_RELAY_TAILSCALE_BIN, to its absolute path." };
}

async function cmdInit(args: Args, env: Env, io: Io, deps: CliDeps): Promise<number> {
	const yes = args.flags.has("yes");
	const ask = new Prompter(io, yes);
	const file = configFilePath(env);

	// 1. omp
	let existing: Record<string, unknown> | null;
	try {
		existing = readConfigFile(file);
	} catch (err) {
		io.err(`${err instanceof ConfigError ? err.message : String(err)}\n`);
		return 1;
	}
	// The effective configuration is validated before anything it names (omp.bin) is executed.
	try {
		if (existing !== null) loadConfig(env);
	} catch (err) {
		io.err(`${err instanceof ConfigError ? err.message : String(err)}\n`);
		return 1;
	}
	const configuredBin = (existing?.omp as { bin?: string | null } | undefined)?.bin ?? env.OMP_PRIVATE_RELAY_OMP_BIN ?? null;
	const ompBin = findOmp(configuredBin, env);
	if (!ompBin) {
		io.err("omp was not found. Install omp first, or put its absolute path in OMP_BIN.\n");
		return 1;
	}
	const info = probeOmp(ompBin);
	if (!info.version) {
		io.err(`${ompBin} did not report a version (expected omp/<version> from \`--version\`).\n`);
		return 1;
	}
	io.out(`omp ${info.version} at ${ompBin}\n`);

	// 2. config file
	if (existing === null) {
		const adapter = (args.flags.get("adapter") as string | undefined) ?? "token";
		if (adapter !== "token" && adapter !== "tailscale-serve") {
			io.err("--adapter must be token or tailscale-serve\n");
			return 2;
		}
		const hostFlag = args.flags.get("host-port");
		const guestFlag = args.flags.get("guest-port");
		const guestPort = guestFlag ? Number(guestFlag) : await firstFreePort(DEFAULT_PORTS.guestPort, []);
		const hostPort = hostFlag ? Number(hostFlag) : await firstFreePort(DEFAULT_PORTS.hostPort, [guestPort]);
		const doc = minimalFile({ omp: { bin: ompBin }, listen: { hostPort, guestPort }, auth: { adapter } });
		io.out(`\nThis creates ${file} (mode 600) and the private directories next to it:\n${JSON.stringify(doc, null, "\t")}\n`);
		io.out(`  ${configDir(env)}/pairing and ${configDir(env)}/check are created when first used, with mode 700.\n`);
		if (!(await ask.confirm("Write it?"))) {
			io.out("Nothing written.\n");
			return 1;
		}
		writeConfigFile(file, doc, env);
		io.out(`wrote ${file}\n`);
	} else io.out(`using the existing ${file}\n`);

	let config: Config;
	try {
		config = loadConfig(env);
	} catch (err) {
		io.err(`${err instanceof ConfigError ? err.message : String(err)}\n`);
		return 1;
	}
	io.out(`adapter: ${config.auth.adapter}; host listener 127.0.0.1:${config.listen.hostPort}, guest listener 127.0.0.1:${config.listen.guestPort}\n`);

	// 3. web client
	let incomplete = false;
	if (!args.flags.has("skip-web")) {
		const outcome = await installWeb(config, info.version, args, env, io, ask, deps);
		if (outcome === "failed") return 1;
		incomplete = outcome === "unavailable";
	}

	// 4. omp settings: shown, and applied only with consent
	const { lines, note } = await ompSettingLines(config);
	io.out(`\nomp reads these two settings when a session starts hosting. Set them with:\n${lines.map(l => `  ${l}`).join("\n")}\n`);
	if (note) io.out(`${note}\n`);
	const flagged = args.flags.has("set-omp-settings");
	if (flagged || (await ask.confirm("Run those two commands now?", false))) {
		for (const line of lines) {
			const argv = line.split(" ").slice(1);
			const r = Bun.spawnSync([ompBin, ...argv], { stdout: "pipe", stderr: "pipe", stdin: "ignore", env: envForOmp(), timeout: 30_000 });
			io.out(`${line}: ${r.exitCode === 0 ? "done" : `failed (exit ${r.exitCode})`}\n`);
			if (r.exitCode !== 0) return 1;
		}
		io.out("Sessions that are already running keep their old relay until you run /collab stop and /collab in them, or restart them.\n");
	} else {
		io.out("Not changed. To undo later: omp config reset collab.relayUrl; omp config reset collab.webUrl\n");
	}

	// 5. what to do next
	const next = [`Start the relay (foreground): ${path.join(pluginRoot(), "bin", "omp-private-relay")} run`, "  or keep it running with omp-services: omp-private-relay dropins --write, then omp-services init"];
	if (config.auth.adapter === "tailscale-serve") {
		next.push(`Expose it on your tailnet: tailscale serve --bg --https=${config.auth.tailscale.servePort ?? config.listen.guestPort} http://127.0.0.1:${config.listen.guestPort}`);
	}
	if (config.pairing.mode === "own") next.push("Pair a browser: omp-private-relay pair");
	next.push("Check it: omp-private-relay doctor");
	io.out(`\nNext:\n${next.map(l => `  ${l}`).join("\n")}\n`);
	if (incomplete) {
		io.out("\nThe web client is not installed yet, so init is not finished: see the message above.\n");
		return 1;
	}
	return 0;
}

/** A regular file read through one descriptor with a size bound (no links followed, no FIFO opened for reading blocks). */
function readRegularFile(file: string, limit: number): Buffer {
	const fd = fs.openSync(file, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0) | (fs.constants.O_NONBLOCK ?? 0));
	try {
		const st = fs.fstatSync(fd);
		if (!st.isFile()) throw new WebAssetError("that is not a regular file");
		if (st.size > limit) throw new WebAssetError("that file is larger than a web client can be");
		// the file can grow after it was measured: the read itself is bounded
		const data = readFdBounded(fd, limit);
		if (data === null) throw new WebAssetError("that file is larger than a web client can be");
		return data;
	} finally {
		fs.closeSync(fd);
	}
}

async function installWeb(config: Config, ompVersion: string, args: Args, env: Env, io: Io, ask: Prompter, deps: CliDeps): Promise<"ok" | "unavailable" | "failed"> {
	const minor = minorOf(ompVersion);
	let builds: WebBuilds;
	try {
		builds = deps.builds ?? loadWebBuilds();
	} catch (err) {
		io.err(`cannot read web-builds.json: ${String(err)}\n`);
		return "failed";
	}
	const build = builds.builds[minor];
	const have = inspectInstalled(config.web.dir);
	if (have.state === "ok" && build && have.meta.treeSha256 === build.treeSha256 && have.meta.noticeSha256 === build.noticeSha256) {
		io.out(`web client for omp ${minor} is already installed in ${config.web.dir}\n`);
		return "ok";
	}
	if (!build) {
		io.out(
			`\nThis plugin version has no prebuilt web client for omp ${minor} (it has: ${Object.keys(builds.builds).join(", ") || "none"}).\n` +
				"Build one from an omp source checkout with: omp-private-relay rebuild-web --omp-src <checkout> (see the README), or wait for a plugin release that lists it.\n",
		);
		return "unavailable";
	}
	const fileFlag = args.flags.get("asset-file");
	let data: Buffer;
	if (typeof fileFlag === "string") {
		io.out(`\nUsing the web client file ${fileFlag}; it is checked against sha256 ${build.sha256}.\n`);
		try {
			data = readRegularFile(fileFlag, MAX_ASSET_BYTES);
		} catch {
			io.err(`cannot read ${fileFlag}\n`);
			return "failed";
		}
	} else {
		const base = (args.flags.get("asset-base") as string | undefined) ?? env.OMP_PRIVATE_RELAY_ASSET_BASE;
		const url = assetUrl(builds, build, base);
		io.out(`\nThe web client for omp ${minor} (${(build.bytes / 1024).toFixed(0)} KiB) is downloaded from:\n  ${url}\nand checked against sha256 ${build.sha256} before anything is written to ${config.web.dir}.\n`);
		if (!(await ask.confirm("Download it?"))) {
			io.out("Not downloaded. Re-run init, or pass --asset-file with a file you fetched yourself.\n");
			return "unavailable";
		}
		try {
			data = await download(url, deps.fetcher);
		} catch (err) {
			io.err(`${err instanceof WebAssetError ? err.message : String(err)}\n`);
			return "failed";
		}
	}
	try {
		const files = verifyAsset(data, build);
		const meta = installTree(files, build, config.web.dir);
		io.out(`installed web client ${meta.ompTag} (${files.size} files, content hash verified) in ${config.web.dir}\n`);
		return "ok";
	} catch (err) {
		io.err(`${err instanceof WebAssetError ? err.message : String(err)}\n`);
		return "failed";
	}
}

/** A number from an untrusted program, or "?": nothing else reaches the terminal. */
function plain(value: unknown): string {
	return typeof value === "number" && Number.isFinite(value) ? String(value) : "?";
}

async function cmdStatus(args: Args, env: Env, io: Io): Promise<number> {
	const config = loadConfig(env);
	const installed = inspectInstalled(config.web.dir);
	// The counts are believed only when they carry a MAC over exactly those bytes under a fresh nonce (relay-check/1): the proof
	// authenticates the answer itself, so a listener that replaced the relay between two requests cannot be taken for it.
	let running: Record<string, unknown> | null = null;
	let answered = false;
	const key = readCheckKey(config.checkDir);
	if (key.ok) {
		const proven = await fetchProvenStats(config.listen.hostPort, key.key);
		if (proven.ok) running = proven.stats;
	}
	try {
		const r = await fetch(`http://127.0.0.1:${config.listen.hostPort}/healthz`, { signal: AbortSignal.timeout(2_000), redirect: "manual" });
		answered = r.status > 0;
		await r.body?.cancel().catch(() => {});
	} catch {
		answered = false;
	}
	let devices: number | null = null;
	if (config.pairing.mode === "own") {
		try {
			devices = new PairingWriter(config.pairing.dir).devices().length;
		} catch {
			devices = null;
		}
	}
	const report = {
		version: version(),
		configFile: configFilePath(env),
		configFileExists: fs.existsSync(configFilePath(env)),
		adapter: config.auth.adapter,
		pairing: config.pairing.mode,
		hostPort: config.listen.hostPort,
		guestPort: config.listen.guestPort,
		relay: running
			? { running: true, proven: true, rooms: plain(running.rooms), guests: plain(running.guests), ready: running.ready === true, uptimeS: plain(running.uptimeS) }
			: answered
				? { running: false, answers: true, proven: false }
				: { running: false },
		web: installed.state === "ok" || installed.state === "modified" || installed.state === "legacy" ? { state: installed.state, ompTag: installed.meta.ompTag } : { state: installed.state },
		pairedBrowsers: devices,
		ompSettingsToUse: (await ompSettingLines(config)).lines,
	};
	if (args.flags.has("json")) io.out(`${JSON.stringify(report, null, 2)}\n`);
	else {
		io.out(`omp-private-relay ${report.version}\n`);
		io.out(`config:   ${report.configFile}${report.configFileExists ? "" : " (not created yet: run init)"}\n`);
		io.out(`adapter:  ${report.adapter}; pairing: ${report.pairing}; ports: host ${report.hostPort}, guest ${report.guestPort}\n`);
		io.out(`relay:    ${running ? `running (proved by its check key), ${report.relay.rooms} room(s), ${report.relay.guests} guest(s)` : answered ? "something answers on the host port but does not prove to be this relay: run doctor" : "not running"}\n`);
		io.out(`web:      ${installed.state === "ok" ? `${(report.web as { ompTag: string }).ompTag}, verified` : installed.state === "legacy" ? "not verified (installed by an older version): run init" : installed.state}\n`);
		if (devices !== null) io.out(`paired:   ${devices} browser(s)\n`);
		io.out(`omp settings to use:\n${report.ompSettingsToUse.map(l => `  ${l}`).join("\n")}\n`);
	}
	return 0;
}

async function cmdPair(env: Env, io: Io): Promise<number> {
	const config = loadConfig(env);
	if (config.pairing.mode !== "own") {
		io.err("pairing.mode is shared: browsers are paired by the program that manages the pairing store.\n");
		return 1;
	}
	// The link carries a bearer secret to whatever answers on the guest port, so that port must prove to be this relay first
	// (relay-check/1). This leaves the gap between this check and the browser opening the link; the README says so.
	const key = readCheckKey(config.checkDir);
	if (!key.ok) {
		io.err(`No link was made: cannot check that the guest port is this relay (${key.reason}). Start the relay first.\n`);
		return 1;
	}
	const proof = await proveListener(config.listen.guestPort, key.key);
	if (!proof.ok) {
		io.err(`No link was made: 127.0.0.1:${config.listen.guestPort} does not prove to be this relay (${proof.reason}). Something else may be holding the port; run: omp-private-relay doctor\n`);
		return 1;
	}
	const auth = createAuth(config);
	const run = auth.start({ log() {}, dropGuests() {} });
	await auth.settled();
	run.stop();
	if (!auth.pairingLink("x")) {
		io.err("Tailscale did not tell this machine's name, so there is no address to print. Start Tailscale, set auth.tailscale.bin (or OMP_PRIVATE_RELAY_TAILSCALE_BIN) to the absolute path of the tailscale command if it cannot be found, or set auth.tailscale.publicHost and owner in the config. No link was made.\n");
		return 1;
	}
	// Only now is a token created: a failure above must not leave one behind.
	const token = await new PairingWriter(config.pairing.dir).createToken();
	const link = auth.pairingLink(token);
	io.out(`Open this link in the browser you want to pair. It works once, for 5 minutes. It is a secret: do not paste it into a log, a chat or a shared terminal.\n\n  ${link}\n\n`);
	if (config.auth.adapter === "token") {
		io.out(`The browser must reach 127.0.0.1:${config.listen.guestPort} on this machine: open it here, or from another machine through\n  ssh -L ${config.listen.guestPort}:127.0.0.1:${config.listen.guestPort} <this-machine>\n`);
	}
	return 0;
}

async function cmdDevices(env: Env, io: Io): Promise<number> {
	const config = loadConfig(env);
	const devices = new PairingWriter(config.pairing.dir).devices();
	if (devices.length === 0) io.out("no paired browsers\n");
	for (const d of devices) io.out(`${d.id}  ${d.pairedAt ? new Date(d.pairedAt * 1000).toISOString().slice(0, 16).replace("T", " ") : "unknown date"}  ${d.label}\n`);
	return 0;
}

async function cmdRevoke(args: Args, env: Env, io: Io): Promise<number> {
	const target = args.positional[1];
	if (!target) throw new UsageError("revoke needs an id from `devices`, or `all`");
	const config = loadConfig(env);
	if (config.pairing.mode !== "own") {
		io.err("pairing.mode is shared: revoke browsers in the program that manages the pairing store.\n");
		return 1;
	}
	const n = await new PairingWriter(config.pairing.dir).revoke(target === "all" ? null : target);
	io.out(n === 0 ? "no browser matched\n" : `unpaired ${n} browser(s); open pages are closed within a few seconds\n`);
	return n === 0 ? 1 : 0;
}

async function cmdRebuild(args: Args, env: Env, io: Io, deps: CliDeps): Promise<number> {
	const config = loadConfig(env);
	const ompBin = findOmp(config.omp.bin, env);
	if (!ompBin) {
		io.err("omp was not found\n");
		return 1;
	}
	const info = probeOmp(ompBin);
	const src = (args.flags.get("omp-src") as string | undefined) ?? env.OMP_SRC;
	if (!src || !info.version) {
		io.err("rebuild-web needs --omp-src <omp source checkout that has release tags> (or OMP_SRC) and a working omp\n");
		return 2;
	}
	const tag = (args.flags.get("tag") as string | undefined) ?? `v${info.version}`;
	// The web directory's parent does not exist on a fresh machine (init is what would have created it).
	ensurePrivateDir(path.dirname(config.web.dir), { tighten: false });
	const out = fs.mkdtempSync(path.join(path.dirname(config.web.dir), ".build-"));
	try {
		const script = deps.buildScript ?? path.join(pluginRoot(), "scripts", "build-web.sh");
		const r = Bun.spawnSync(["bash", script, "--omp-src", src, "--tag", tag, "--omp-bin", ompBin, "--out", path.join(out, "dist")], {
			stdout: "inherit",
			stderr: "inherit",
			stdin: "ignore",
			env: envForBun(env),
		});
		if (r.exitCode !== 0) return 1;
		const files = readTree(path.join(out, "dist"));
		const notice = fs.readFileSync(path.join(pluginRoot(), "NOTICE"));
		files.set("NOTICE", notice);
		const hash = treeHash(files);
		const minor = minorOf(tag.replace(/^v/, ""));
		const pinned = loadWebBuilds().builds[minor];
		const bunVersion = Bun.spawnSync([ompBin, "--version"], { stdout: "pipe", env: envForBun(env) }).stdout.toString().trim();
		const meta: InstalledBuild = { ompTag: tag, bun: bunVersion, treeSha256: hash, noticeSha256: sha256(notice), asset: "(built locally)", installedAt: "" };
		installTree(files, { ompTag: tag, bun: bunVersion, asset: meta.asset, sha256: "", treeSha256: hash, noticeSha256: sha256(notice), bytes: 0, files: files.size }, config.web.dir);
		io.out(`built ${tag} with Bun ${bunVersion} and installed it in ${config.web.dir}\n`);
		io.out(pinned ? (pinned.treeSha256 === hash ? "content hash matches the build this plugin pins\n" : "content hash differs from the build this plugin pins (expected when the omp patch level or Bun differs)\n") : "this plugin version pins no build for that omp minor\n");
		return 0;
	} finally {
		fs.rmSync(out, { recursive: true, force: true });
	}
}

async function cmdDropins(args: Args, env: Env, io: Io): Promise<number> {
	const config = loadConfig(env);
	const ompBin = findOmp(config.omp.bin, env);
	if (!ompBin) {
		io.err("omp was not found\n");
		return 1;
	}
	// The service and the health command run without this shell's environment: what they read is the config file, so every
	// setting has to be in it. An override set here would make the drop-ins describe a different relay than the one that runs.
	const stray = strayOverrides(env);
	if (stray.length > 0) {
		io.err(`These variables are set here, but a service would not see them: ${stray.join(", ")}. Put the settings in the config file (or unset them), then run dropins again.\n`);
		return 1;
	}
	if (!fs.existsSync(configFilePath(env))) {
		io.err(`There is no config file (${configFilePath(env)}): run init first, so the service has one to read.\n`);
		return 1;
	}
	const paths = dropinPaths(env);
	const docs = [
		[paths.services, servicesDropin({ config, ompBin, env })],
		[paths.health, healthDropin({ config, ompBin, env })],
	] as const;
	if (!args.flags.has("write")) {
		for (const [file, doc] of docs) io.out(`# ${file}\n${JSON.stringify(doc, null, "\t")}\n\n`);
		io.out("Nothing written. Add --write to create these two files (0600, in 0700 directories), then run omp-services init.\n");
		return 0;
	}
	for (const [file, doc] of docs) io.out(`wrote ${writeDropin(file, doc as Record<string, unknown>)}\n`);
	return 0;
}

export async function runCli(argv: string[], env: Env, io: Io, deps: CliDeps = {}): Promise<number> {
	let args: Args;
	try {
		args = parseArgs(argv);
	} catch (err) {
		io.err(`${(err as Error).message}\n`);
		return 2;
	}
	const command = args.positional[0];
	if (args.flags.has("help") || command === "-h") {
		io.out(HELP);
		return 0;
	}
	try {
		switch (command) {
			case "init":
				return await cmdInit(args, env, io, deps);
			case "status":
				return await cmdStatus(args, env, io);
			case "doctor": {
				const findings = await runDoctor(env);
				io.out(args.flags.has("json") ? `${JSON.stringify(findings, null, 2)}\n` : `${renderFindings(findings)}\n`);
				return findings.some(f => f.level === "fail") ? 1 : 0;
			}
			case "check": {
				const r = await runCheck(env);
				io.out(`${r.message}\n`);
				return r.ok ? 0 : 1;
			}
			case "pair":
				return await cmdPair(env, io);
			case "devices":
				return await cmdDevices(env, io);
			case "revoke":
				return await cmdRevoke(args, env, io);
			case "rebuild-web":
				return await cmdRebuild(args, env, io, deps);
			case "dropins":
				return await cmdDropins(args, env, io);
			case "run": {
				const code = runRelay();
				if (code !== null) return code;
				await Promise.withResolvers<never>().promise; // the relay keeps the process alive until SIGINT/SIGTERM
				return 0;
			}
			case undefined:
			case "help":
			case "--help":
				io.out(HELP);
				return command === undefined ? 2 : 0;
			default:
				io.err(`unknown command: ${command}\n\n${HELP}`);
				return 2;
		}
	} catch (err) {
		if (err instanceof UsageError) {
			io.err(`${err.message}\n`);
			return 2;
		}
		if (err instanceof ConfigError || err instanceof PairingError || err instanceof WebAssetError) {
			io.err(`${err.message}\n`);
			return 1;
		}
		throw err;
	}
}

if (import.meta.main) {
	const rl = readline.createInterface({ input: process.stdin });
	const lines = rl[Symbol.asyncIterator]();
	const io: Io = {
		out: text => process.stdout.write(text),
		err: text => process.stderr.write(text),
		interactive: Boolean(process.stdin.isTTY),
		line: async () => {
			const next = await lines.next();
			return next.done ? null : next.value;
		},
	};
	const code = await runCli(Bun.argv.slice(2), process.env, io);
	rl.close();
	process.exit(code);
}
