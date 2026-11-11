// `doctor`, `check` and `status`: what is installed, what is running, and whether what answers on the relay's ports is
// really the relay of this user. Nothing here changes anything.
import * as fs from "node:fs";
import * as path from "node:path";
import { parseTailscaleStatus } from "./auth/tailscale-serve";
import { type Config, ConfigError, configFilePath, loadConfig, relayUrl } from "./config";
import { proveListener, readCheckKey } from "./listener-check";
import { PairingReader } from "./pairing-store";
import { ownership } from "./platform";
import { checkBun, envForOmp, findOmp, findTailscale, MIN_BUN, minorOf, probeOmp, TAILSCALE_PLACES } from "./runtime";
import { parseServeStatus } from "./serve-status";
import { inspectInstalled, loadWebBuilds } from "./web-asset";
import { UID } from "./fsutil";
import { servedPathProblem } from "./trustwalk";

export type Level = "ok" | "warn" | "fail";

export interface Finding {
	id: string;
	level: Level;
	message: string;
	fix?: string;
}

const finding = (id: string, level: Level, message: string, fix?: string): Finding => ({ id, level, message, ...(fix ? { fix } : {}) });

type Env = Record<string, string | undefined>;

function runText(argv: string[], env?: Env): { code: number; out: string } | null {
	try {
		const r = Bun.spawnSync(argv, { stdout: "pipe", stderr: "ignore", stdin: "ignore", env: env ?? envForOmp(), timeout: 10_000 });
		return { code: r.exitCode ?? -1, out: r.stdout.toString() };
	} catch {
		return null;
	}
}

/** The two listeners: is it ours, and does it prove to be this relay? */
async function listenerFindings(config: Config): Promise<Finding[]> {
	const out: Finding[] = [];
	const key = readCheckKey(config.checkDir);
	for (const [label, port] of [
		["host listener", config.listen.hostPort],
		["guest listener", config.listen.guestPort],
	] as const) {
		const id = label.split(" ")[0] === "host" ? "host-port" : "guest-port";
		const owner = ownership(port, UID);
		let answers = false;
		try {
			await fetch(`http://127.0.0.1:${port}/`, { signal: AbortSignal.timeout(2_000), redirect: "manual" });
			answers = true;
		} catch {
			answers = false;
		}
		if (!answers) {
			out.push(finding(id, owner.state === "none" || owner.state === "unknown" ? "warn" : "fail", `${label} (127.0.0.1:${port}) is not answering; the relay is not running`, "start it: omp-private-relay run (see the README, \"Run it\")"));
			continue;
		}
		if (owner.state === "foreign") {
			out.push(finding(id, "fail", `${label} (127.0.0.1:${port}) is held by another account's process`, "stop whatever holds the port, or choose other ports in the config file"));
			continue;
		}
		if (!key.ok) {
			out.push(finding(id, "fail", `${label} (127.0.0.1:${port}) answers, but its proof cannot be checked: ${key.reason}`));
			continue;
		}
		const proof = await proveListener(port, key.key);
		if (!proof.ok) out.push(finding(id, "fail", `${label} (127.0.0.1:${port}) does not prove to be this relay: ${proof.reason}`, "something else holds the port; stop it and restart the relay"));
		else if (owner.state === "ours") out.push(finding(id, "ok", `${label} (127.0.0.1:${port}): this relay, run by you`));
		else out.push(finding(id, "ok", `${label} (127.0.0.1:${port}): this relay (proved by its check key); the platform tool could not show the owner`));
	}
	return out;
}

export async function runDoctor(env: Env = process.env): Promise<Finding[]> {
	const out: Finding[] = [];
	const file = configFilePath(env);
	let config: Config;
	try {
		config = loadConfig(env);
		out.push(finding("config", "ok", fs.existsSync(file) ? `configuration ${file} is valid` : "no config file yet: the defaults apply", fs.existsSync(file) ? undefined : "run: omp-private-relay init"));
	} catch (err) {
		out.push(finding("config", "fail", err instanceof ConfigError ? err.message : `configuration unreadable: ${String(err)}`));
		return out;
	}

	const tooOld = checkBun(Bun.version);
	out.push(tooOld ? finding("runtime", "fail", tooOld, "run through omp's embedded Bun (bin/omp-private-relay does), and use omp 18.18 or newer") : finding("runtime", "ok", `running on Bun ${Bun.version} (needs ${MIN_BUN} or newer)`));
	const omp = findOmp(config.omp.bin, env);
	let ompMinor: string | null = null;
	if (!omp) out.push(finding("omp", "fail", "omp was not found", "install omp, or set omp.bin in the config file"));
	else {
		const info = probeOmp(omp);
		if (!info.version) out.push(finding("omp", "fail", `${omp} did not report a version`, "check that it is the omp binary (not a wrapper that sets BUN_BE_BUN)"));
		else {
			ompMinor = minorOf(info.version);
			out.push(finding("omp", "ok", `omp ${info.version} at ${omp}`));
		}
	}

	const installed = inspectInstalled(config.web.dir);
	const unsafeWeb = servedPathProblem(config.web.dir);
	if (unsafeWeb) out.push(finding("web-dir-safe", "fail", `the web client is served from a directory another account could replace: ${unsafeWeb}`, "keep it under a directory only you (and root) can write to; the relay will not serve it until then"));
	if (installed.state === "missing") out.push(finding("web", "fail", `the web client is not installed in ${config.web.dir}`, "run: omp-private-relay init"));
	else if (installed.state === "unmanaged") out.push(finding("web", "warn", `${config.web.dir}: ${installed.reason}`));
	else if (installed.state === "legacy") out.push(finding("web", "fail", `the installed web client was recorded by an older version, without a NOTICE hash, so it cannot be certified`, "run: omp-private-relay init"));
	else if (installed.state === "modified") out.push(finding("web", "fail", `the installed web client differs from its recorded content hash (it was edited or damaged)`, "run: omp-private-relay init"));
	else {
		const builtMinor = minorOf(installed.meta.ompTag.replace(/^v/, ""));
		if (ompMinor && builtMinor !== ompMinor) {
			out.push(finding("web", "warn", `the web client was built for omp ${builtMinor}, but this is omp ${ompMinor}`, "run: omp-private-relay init (or rebuild-web)"));
		} else out.push(finding("web", "ok", `web client ${installed.meta.ompTag} (built with Bun ${installed.meta.bun}), content hash verified`));
		try {
			const known = loadWebBuilds().builds[builtMinor];
			if (known && known.treeSha256 !== installed.meta.treeSha256) {
				out.push(finding("web-pinned", "warn", "the installed web client is not the build this plugin version pins (a local rebuild, or an older plugin version)"));
			}
		} catch {
			// no web-builds.json: reported by init when it is needed
		}
	}

	// Pairing health is what the relay itself would see: the reader's checks (modes, owner, links), never the writer's.
	const view = new PairingReader(config.pairing.dir).read();
	if (view.refusal === "store-missing" && config.pairing.mode === "own") {
		// Normal until the first browser is paired: the relay's own store is created by `pair` and filled when a browser redeems the link.
		out.push(finding("pairing", "warn", `no browser is paired yet (${config.pairing.dir} does not exist or has no devices yet)`, "run: omp-private-relay pair"));
	} else if (view.refusal) {
		out.push(finding("pairing", "fail", `the relay would refuse the pairing store ${config.pairing.dir}: ${view.refusal}`, "check its owner and modes (pairing-store/1): directory 0700, devices.json 0600, no symlinks"));
	} else if (config.pairing.mode === "own") {
		out.push(finding("pairing", view.hashes.size > 0 ? "ok" : "warn", `${view.hashes.size} paired browser(s) in ${config.pairing.dir}`, view.hashes.size > 0 ? undefined : "run: omp-private-relay pair"));
	} else out.push(finding("pairing", "ok", `shared pairing store: ${view.hashes.size} paired browser(s)`));

	out.push(...(await listenerFindings(config)));

	if (config.auth.adapter === "tailscale-serve") out.push(...tailscaleFindings(config, env));

	if (omp) {
		const wantRelay = relayUrl(config);
		const have = runText([omp, "config", "get", "collab.relayUrl"]);
		const value = have?.code === 0 ? have.out.trim() : null;
		if (value === null) out.push(finding("omp-settings", "warn", "could not read omp's collab.relayUrl"));
		else if (value !== wantRelay) out.push(finding("omp-settings", "warn", `omp's collab.relayUrl is ${value || "(unset)"}, not ${wantRelay}: sessions will not use this relay`, `omp config set collab.relayUrl ${wantRelay}`));
		else out.push(finding("omp-settings", "ok", `omp's collab.relayUrl points at this relay (${wantRelay})`));
	}
	return out;
}

function tailscaleFindings(config: Config, env: Env): Finding[] {
	const out: Finding[] = [];
	const configured = config.auth.tailscale.bin;
	const bin = findTailscale(configured, env);
	if (!bin) {
		const looked = path.isAbsolute(configured) ? configured : `PATH and ${TAILSCALE_PLACES.join(", ")}`;
		out.push(finding("tailscale", "fail", `the tailscale command was not found (looked at ${looked})`, "install Tailscale, or set auth.tailscale.bin in the config file (or OMP_PRIVATE_RELAY_TAILSCALE_BIN) to its absolute path, for example /Applications/Tailscale.app/Contents/MacOS/Tailscale"));
		return out;
	}
	const status = runText([bin, "status", "--json"]);
	const self = status?.code === 0 ? parseTailscaleStatus(status.out) : null;
	if (!status) {
		out.push(finding("tailscale", "fail", `cannot run ${bin}`, "install Tailscale, or set auth.tailscale.bin"));
		return out;
	}
	if (status.code !== 0) {
		out.push(finding("tailscale", "fail", "tailscale is installed but not answering (is it logged in and running?)"));
		return out;
	}
	if (!self) {
		out.push(finding("tailscale", "fail", "`tailscale status --json` did not name this machine and its owner"));
		return out;
	}
	out.push(finding("tailscale", "ok", `Tailscale: this machine is ${self.host}, owner ${self.login}`));
	const guest = config.listen.guestPort;
	const servePort = config.auth.tailscale.servePort ?? guest;
	const serve = runText([bin, "serve", "status", "--json"]);
	if (!serve || serve.code !== 0) {
		out.push(finding("serve-route", "warn", "could not read `tailscale serve status`"));
		return out;
	}
	const route = parseServeStatus(serve.out, servePort, `http://127.0.0.1:${guest}`);
	const command = `tailscale serve --bg --https=${servePort} http://127.0.0.1:${guest}`;
	if (route.state === "ok") {
		out.push(route.funnel ? finding("serve-route", "fail", `the Serve route on https port ${servePort} is exposed through Funnel (the public internet)`, `tailscale funnel --https=${servePort} off`) : finding("serve-route", "ok", `tailscale serve forwards https port ${servePort} to the guest listener (tailnet only)`));
	} else if (route.state === "absent") out.push(finding("serve-route", "warn", `no Serve route on https port ${servePort}`, command));
	else if (route.state === "elsewhere") out.push(finding("serve-route", "fail", `https port ${servePort} is served, but to ${route.target}, not to this relay`, command));
	else out.push(finding("serve-route", "warn", "tailscale serve status is not readable JSON"));
	return out;
}

/** `check`: a quiet yes/no for the health drop-in. Both listeners must prove to be this relay, run by this user. */
export async function runCheck(env: Env = process.env): Promise<{ ok: boolean; message: string }> {
	let config: Config;
	try {
		config = loadConfig(env);
	} catch {
		return { ok: false, message: "invalid-config" };
	}
	const key = readCheckKey(config.checkDir);
	if (!key.ok) return { ok: false, message: "no-check-key" };
	for (const port of [config.listen.hostPort, config.listen.guestPort]) {
		const owner = ownership(port, UID);
		if (owner.state === "foreign") return { ok: false, message: "owner-mismatch" };
		const proof = await proveListener(port, key.key);
		if (!proof.ok) return { ok: false, message: "proof-failed" };
	}
	return { ok: true, message: "ok" };
}

export function renderFindings(findings: Finding[]): string {
	const tag = { ok: "ok  ", warn: "warn", fail: "FAIL" } as const;
	return findings.map(f => `[${tag[f.level]}] ${f.id}: ${f.message}${f.fix ? `\n       fix: ${f.fix}` : ""}`).join("\n");
}
