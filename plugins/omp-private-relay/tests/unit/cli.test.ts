// The command line, in process: init (every step asks, nothing is edited silently, the download is stubbed), and the
// pairing and diagnosis commands against a real relay on free ports. A fake omp records what it is asked.
import { afterAll, afterEach, beforeAll, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import { type CliDeps, type Io, parseArgs, runCli } from "../../src/cli";
import { DEFAULT_PORTS } from "../../src/config";
import { PairingWriter } from "../../src/pairing-store";
import { packTar, sha256, treeHash, type WebBuild, type WebBuilds } from "../../src/web-asset";
import { writeFakeOmp } from "./support/fake-omp";
import { closedWithin, COOKIE_NAME, freePorts, guestHeaders, guestPeer, hostPeer, makeRoomId, OWNER, PUBLIC_HOST, type Relay, rawRequest, rawStatus, startRelay } from "./support/harness";

const roots: string[] = [];
const scratch = (): string => {
	const r = fs.mkdtempSync(path.join(os.tmpdir(), "oprc-"));
	roots.push(r);
	return r;
};
afterEach(() => {
	for (const r of roots.splice(0)) fs.rmSync(r, { recursive: true, force: true });
});

/** An Io that records output and answers prompts from a script (end of input once the script is used up). */
function io(answers: string[] = []): Io & { stdout: string; stderr: string } {
	const queue = [...answers];
	const self = {
		stdout: "",
		stderr: "",
		out(t: string) {
			self.stdout += t;
		},
		err(t: string) {
			self.stderr += t;
		},
		async line() {
			return queue.length ? (queue.shift() as string) : null;
		},
	};
	return self;
}

const files = (): Map<string, Buffer> => new Map([["index.html", Buffer.from("<!doctype html><title>t</title>")], ["abcd1234.js", Buffer.from("console.log(1)")], ["NOTICE", Buffer.from("notice")]]);

function pinned(minor = "18.18"): { builds: WebBuilds; tar: Buffer; build: WebBuild } {
	const tar = packTar(files());
	const build: WebBuild = { ompTag: `v${minor}.0`, bun: "1.4.4", asset: `omp-private-relay-web-${minor}.tar`, sha256: sha256(tar), treeSha256: treeHash(files()), noticeSha256: sha256(files().get("NOTICE") as Buffer), bytes: tar.length, files: 3 };
	return { builds: { schema: 1, repository: "owner/repo", releaseTag: "omp-private-relay-v9.9.9", builds: { [minor]: build } }, tar, build };
}

function snapshot(root: string): string[] {
	const out: string[] = [];
	const walk = (d: string) => {
		for (const e of fs.readdirSync(d, { withFileTypes: true })) {
			const p = path.join(d, e.name);
			out.push(path.relative(root, p));
			if (e.isDirectory()) walk(p);
		}
	};
	walk(root);
	return out.sort();
}

function setup(version = "18.18.0") {
	const root = scratch();
	const home = path.join(root, "home");
	fs.mkdirSync(home, { mode: 0o700 });
	const fake = writeFakeOmp(root, { version });
	const env = { HOME: home, OMP_BIN: fake.bin, PATH: "/usr/bin:/bin" };
	return { root, home, fake, env };
}

describe("argument parsing", () => {
	test("flags with and without values, and = forms", () => {
		const a = parseArgs(["init", "--yes", "--guest-port", "20001", "--adapter=token", "--skip-web"]);
		expect(a.positional).toEqual(["init"]);
		expect(a.flags.get("yes")).toBe(true);
		expect(a.flags.get("guest-port")).toBe("20001");
		expect(a.flags.get("adapter")).toBe("token");
		expect(() => parseArgs(["init", "--guest-port"])).toThrow(/needs a value/);
		expect(() => parseArgs(["init", "--guest-port", "--yes"])).toThrow(/needs a value/);
	});
});

describe("help and unknown commands", () => {
	test("help exits 0, a missing or unknown command exits 2 and says what is available", async () => {
		const a = io();
		expect(await runCli(["help"], {}, a)).toBe(0);
		expect(a.stdout).toContain("init");
		expect(await runCli([], {}, io())).toBe(2);
		const b = io();
		expect(await runCli(["frobnicate"], {}, b)).toBe(2);
		expect(b.stderr).toContain("unknown command");
	});
	test("--help and -h are help, not an unknown command: exit 0 with the command list", async () => {
		for (const flag of ["--help", "-h"]) {
			const a = io();
			expect(await runCli([flag], {}, a)).toBe(0);
			expect(a.stdout).toContain("init");
			expect(a.stderr).toBe("");
		}
	});
});

describe("init", () => {
	test("with --yes and a local asset: writes the config, installs the verified client, shows the omp lines and does NOT run them", async () => {
		const { builds, tar } = pinned();
		const { root, home, fake, env } = setup();
		const asset = path.join(root, "asset.tar");
		fs.writeFileSync(asset, tar);
		const out = io();
		const [host, guest] = await freePorts(2);
		const code = await runCli(["init", "--yes", "--asset-file", asset, "--host-port", String(host), "--guest-port", String(guest)], env, out, { builds });
		expect(code).toBe(0);
		const configFile = path.join(home, ".config", "omp-private-relay", "config.json");
		expect(fs.statSync(configFile).mode & 0o777).toBe(0o600);
		expect(fs.statSync(path.dirname(configFile)).mode & 0o777).toBe(0o700);
		expect(JSON.parse(fs.readFileSync(configFile, "utf8"))).toEqual({ schemaVersion: 1, omp: { bin: fake.bin }, listen: { hostPort: host, guestPort: guest }, auth: { adapter: "token" } });
		const web = path.join(home, ".local", "share", "omp-private-relay", "web", "current");
		expect(fs.readFileSync(path.join(web, "index.html"), "utf8")).toContain("<title>t</title>");
		expect(out.stdout).toContain(`omp config set collab.relayUrl ws://127.0.0.1:${host}`);
		expect(out.stdout).toContain(`omp config set collab.webUrl http://127.0.0.1:${guest}`);
		expect(out.stdout).toContain("Not changed.");
		expect(fake.calls().filter(c => c.includes("config set"))).toEqual([]); // nothing was edited
		expect(fake.calls().filter(c => c.includes("--version"))).toEqual([]);
		// and nothing was written anywhere else
		const top = snapshot(root).filter(p => p.split(path.sep).length <= 3 && p.startsWith("home"));
		expect(top).toEqual(expect.arrayContaining(["home/.config", "home/.config/omp-private-relay", "home/.local", "home/.local/share"]));
		expect(snapshot(root).filter(p => !p.startsWith("home") && !p.startsWith("fake bin") && p !== "fake-omp.log" && p !== "asset.tar")).toEqual([]);
	});

	test("without --yes, nothing is written until it is asked and answered yes; a no (or no answer) writes nothing at all", async () => {
		const { builds } = pinned();
		const { home, env } = setup();
		const [host, guest] = await freePorts(2);
		const args = ["init", "--host-port", String(host), "--guest-port", String(guest), "--skip-web"];
		const no = io(["n"]);
		expect(await runCli(args, env, no, { builds })).toBe(1);
		expect(no.stdout).toContain("Write it? [y/N]");
		expect(no.stdout).toContain("Nothing written");
		expect(fs.readdirSync(home)).toEqual([]);
		const eof = io();
		expect(await runCli(args, env, eof, { builds })).toBe(1);
		expect(fs.readdirSync(home)).toEqual([]);
		const yes = io(["y", "n"]);
		expect(await runCli(args, env, yes, { builds })).toBe(0);
		expect(fs.existsSync(path.join(home, ".config", "omp-private-relay", "config.json"))).toBe(true);
	});

	test("it asks before running the two omp commands, and runs them (with BUN_BE_BUN cleared) only on a yes", async () => {
		const { builds } = pinned();
		const { fake, env } = setup();
		const [host, guest] = await freePorts(2);
		const base = ["init", "--yes", "--host-port", String(host), "--guest-port", String(guest), "--skip-web"];
		const declined = io(["n"]);
		expect(await runCli(base, { ...env, BUN_BE_BUN: "1" }, declined, { builds })).toBe(0);
		expect(declined.stdout).toContain("Run those two commands now? [y/N]");
		expect(fake.calls().filter(c => c.includes("config set"))).toEqual([]);
		const accepted = io(["y"]);
		expect(await runCli(base, { ...env, BUN_BE_BUN: "1" }, accepted, { builds })).toBe(0);
		expect(fake.calls()).toEqual([`BUN_BE_BUN=unset config set collab.relayUrl ws://127.0.0.1:${host}`, `BUN_BE_BUN=unset config set collab.webUrl http://127.0.0.1:${guest}`]);
		expect(accepted.stdout).toContain("/collab stop");
	});

	test("--yes alone never edits omp's settings; --set-omp-settings does, without asking", async () => {
		const { builds } = pinned();
		const { fake, env } = setup();
		const [host, guest] = await freePorts(2);
		const base = ["init", "--yes", "--host-port", String(host), "--guest-port", String(guest), "--skip-web"];
		await runCli(base, env, io(), { builds });
		expect(fake.calls()).toEqual([]);
		const second = io();
		await runCli([...base, "--set-omp-settings"], env, second, { builds });
		expect(fake.calls()).toHaveLength(2);
		expect(second.stdout).not.toContain("[y/N]");
	});

	test("the download: shown, asked about, checked against the pinned hash, and stubbed here (no network)", async () => {
		const { builds, tar, build } = pinned();
		const { home, env } = setup();
		const [host, guest] = await freePorts(2);
		const base = ["init", "--yes", "--host-port", String(host), "--guest-port", String(guest)];
		const asked: string[] = [];
		const fetcher: CliDeps["fetcher"] = async url => {
			asked.push(url);
			return new Response(new Uint8Array(tar));
		};
		// declining the download leaves init unfinished and installs nothing
		expect(await runCli(["init", "--host-port", String(host), "--guest-port", String(guest)], env, io(["y", "n", "n"]), { builds, fetcher })).toBe(1);
		expect(asked).toEqual([]);
		// accepting: fetched from the release URL derived from the pin, verified, installed
		const ok = io();
		expect(await runCli(base, env, ok, { builds, fetcher })).toBe(0);
		expect(asked).toEqual([`https://github.com/owner/repo/releases/download/omp-private-relay-v9.9.9/${build.asset}`]);
		expect(ok.stdout).toContain(build.sha256);
		expect(fs.existsSync(path.join(home, ".local", "share", "omp-private-relay", "web", "current", "index.html"))).toBe(true);
		// a download whose bytes do not match the pin is refused and nothing new is installed
		const other = packTar(new Map([["index.html", Buffer.from("<script>evil()</script>")]]));
		fs.rmSync(path.join(home, ".local"), { recursive: true });
		const bad = io();
		expect(await runCli(base, env, bad, { builds, fetcher: async () => new Response(new Uint8Array(other)) })).toBe(1);
		expect(bad.stderr).toMatch(/sha256/);
		expect(fs.existsSync(path.join(home, ".local", "share", "omp-private-relay", "web"))).toBe(false);
	});

	test("an asset base from the environment replaces the release URL; plain http to a non-loopback host is refused", async () => {
		const { builds, tar } = pinned();
		const { home, env } = setup();
		const [host, guest] = await freePorts(2);
		const asked: string[] = [];
		const fetcher: CliDeps["fetcher"] = async url => {
			asked.push(url);
			return new Response(new Uint8Array(tar));
		};
		const args = ["init", "--yes", "--host-port", String(host), "--guest-port", String(guest)];
		expect(await runCli(args, { ...env, OMP_PRIVATE_RELAY_ASSET_BASE: "https://mirror.example/files/" }, io(), { builds, fetcher })).toBe(0);
		expect(asked).toEqual(["https://mirror.example/files/omp-private-relay-web-18.18.tar"]);
		fs.rmSync(path.join(home, ".local"), { recursive: true });
		const refused = io();
		expect(await runCli(args, { ...env, OMP_PRIVATE_RELAY_ASSET_BASE: "http://mirror.example/files" }, refused, { builds, fetcher })).toBe(1);
		expect(refused.stderr).toContain("only downloaded over https");
	});

	test("an omp minor with no pinned build: config is written, the way forward is explained, init is not finished", async () => {
		const { builds } = pinned();
		const { home, env } = setup("19.0.2");
		const [host, guest] = await freePorts(2);
		const out = io();
		expect(await runCli(["init", "--yes", "--host-port", String(host), "--guest-port", String(guest)], env, out, { builds })).toBe(1);
		expect(out.stdout).toContain("no prebuilt web client for omp 19.0");
		expect(out.stdout).toContain("rebuild-web");
		expect(fs.existsSync(path.join(home, ".config", "omp-private-relay", "config.json"))).toBe(true);
	});

	test("an already installed matching client is left alone", async () => {
		const { builds, tar } = pinned();
		const { root, env } = setup();
		const asset = path.join(root, "a.tar");
		fs.writeFileSync(asset, tar);
		const [host, guest] = await freePorts(2);
		const args = ["init", "--yes", "--asset-file", asset, "--host-port", String(host), "--guest-port", String(guest)];
		expect(await runCli(args, env, io(), { builds })).toBe(0);
		const again = io();
		expect(await runCli(args, env, again, { builds })).toBe(0);
		expect(again.stdout).toContain("already installed");
		expect(again.stdout).toContain("using the existing");
	});

	test("without omp, or with something that does not report a version, init stops before writing anything", async () => {
		const { builds } = pinned();
		const root = scratch();
		const home = path.join(root, "home");
		fs.mkdirSync(home, { mode: 0o700 });
		const none = io();
		expect(await runCli(["init", "--yes"], { HOME: home, PATH: "/nonexistent" }, none, { builds })).toBe(1);
		expect(none.stderr).toContain("omp was not found");
		const liar = path.join(root, "liar");
		fs.writeFileSync(liar, "#!/bin/sh\necho hello\n", { mode: 0o755 });
		const odd = io();
		expect(await runCli(["init", "--yes"], { HOME: home, OMP_BIN: liar, PATH: "/usr/bin:/bin" }, odd, { builds })).toBe(1);
		expect(odd.stderr).toContain("did not report a version");
		expect(fs.readdirSync(home)).toEqual([]);
	});

	test("the first free ports at or after the defaults are chosen when the defaults are busy", async () => {
		const { builds } = pinned();
		const { env, home } = setup();
		const holder = net.createServer();
		const bound = await new Promise<boolean>(resolve => {
			holder.once("error", () => resolve(false));
			holder.listen(DEFAULT_PORTS.guestPort, "127.0.0.1", () => resolve(true));
		});
		try {
			await runCli(["init", "--yes", "--skip-web"], env, io(), { builds });
			const cfg = JSON.parse(fs.readFileSync(path.join(home, ".config", "omp-private-relay", "config.json"), "utf8"));
			expect(cfg.listen.guestPort).toBeGreaterThanOrEqual(DEFAULT_PORTS.guestPort);
			if (bound) expect(cfg.listen.guestPort).toBeGreaterThan(DEFAULT_PORTS.guestPort);
			expect(cfg.listen.hostPort).not.toBe(cfg.listen.guestPort);
		} finally {
			holder.close();
		}
	});

	test("the tailscale adapter: the web URL names this machine when it is known, a marked placeholder when not", async () => {
		const { builds } = pinned();
		const { env } = setup();
		const [host, guest] = await freePorts(2);
		const known = io();
		await runCli(["init", "--yes", "--skip-web", "--adapter", "tailscale-serve", "--host-port", String(host), "--guest-port", String(guest)], { ...env, OMP_PRIVATE_RELAY_TAILSCALE_OWNER: OWNER, OMP_PRIVATE_RELAY_TAILSCALE_HOST: PUBLIC_HOST }, known, { builds });
		expect(known.stdout).toContain(`omp config set collab.webUrl https://${PUBLIC_HOST}:${guest}`);
		expect(known.stdout).toContain(`tailscale serve --bg --https=${guest} http://127.0.0.1:${guest}`);
		const second = setup();
		const unknown = io();
		await runCli(["init", "--yes", "--skip-web", "--adapter", "tailscale-serve", "--host-port", String(host), "--guest-port", String(guest)], { ...second.env, OMP_PRIVATE_RELAY_TAILSCALE_BIN: "/usr/bin/false" }, unknown, { builds });
		expect(unknown.stdout).toContain("placeholder");
		expect(unknown.stdout).toContain("<this-machine>");
	});

	test("a bad adapter name is a usage error", async () => {
		const { builds } = pinned();
		const { env, home } = setup();
		const out = io();
		expect(await runCli(["init", "--yes", "--skip-web", "--adapter", "open"], env, out, { builds })).toBe(2);
		expect(fs.readdirSync(home)).toEqual([]);
	});
});

describe("pairing and diagnosis against a running relay", () => {
	let relay: Relay;
	let env: Record<string, string>;
	beforeAll(async () => {
		relay = await startRelay({ adapter: "token", env: { OMP_PRIVATE_RELAY_RECHECK_MS: "300" } });
		const fake = writeFakeOmp(relay.root, { version: "18.18.0", relayUrl: "" });
		env = { ...relay.env, OMP_BIN: fake.bin };
	});
	afterAll(async () => relay.stop());

	test("pair prints a one-time link that pairs a browser through the page's own call", async () => {
		const out = io();
		expect(await runCli(["pair"], env, out)).toBe(0);
		const link = /http:\/\/127\.0\.0\.1:\d+\/pair#token=([A-Za-z0-9_-]{43})/.exec(out.stdout);
		expect(link).not.toBeNull();
		expect(out.stdout).toContain("ssh -L");
		const token = (link as RegExpExecArray)[1];
		const post = await rawRequest(relay.guestPort, "/__pair", guestHeaders(relay, { Cookie: null, "Content-Type": "application/json", "X-Omp-Relay": "1" }), { method: "POST", body: JSON.stringify({ token }) });
		expect(post.status).toBe(200);
		const cookie = (post.headers["set-cookie"] as string).split(";")[0] as string;
		expect(cookie.startsWith(`${COOKIE_NAME}=`)).toBe(true);
		expect(await rawStatus(relay.guestPort, "/", guestHeaders(relay, { Cookie: cookie, Origin: null }))).toBe(200);
	});

	test("devices lists, revoke unpairs one by id (and closes its open page) or all; bad arguments are refused", async () => {
		const writer = new PairingWriter(relay.pairingDir);
		const post = await rawRequest(relay.guestPort, "/__pair", guestHeaders(relay, { Cookie: null, "Content-Type": "application/json", "X-Omp-Relay": "1" }), { method: "POST", body: JSON.stringify({ token: await writer.createToken() }) });
		const cookie = (post.headers["set-cookie"] as string).split(";")[0] as string;
		const room = makeRoomId("cli");
		const h = hostPeer(relay, room);
		await h.opened;
		const g = guestPeer(relay, room, { Cookie: cookie });
		expect(await g.opened).toBe(true);
		const list = io();
		expect(await runCli(["devices"], env, list)).toBe(0);
		const mine = writer.devices().filter(d => d.label !== "test").at(-1);
		expect(mine).toBeDefined();
		expect(list.stdout).toContain((mine as { id: string }).id);
		expect(await runCli(["revoke"], env, io())).toBe(2);
		expect(await runCli(["revoke", "zz"], env, io())).toBe(1);
		const revoked = io();
		expect(await runCli(["revoke", (mine as { id: string }).id], env, revoked)).toBe(0);
		expect(revoked.stdout).toContain("unpaired 1");
		expect((await closedWithin(g, 5000))?.code).toBe(4401);
		h.ws.close();
		expect(await runCli(["revoke", "all"], env, io())).toBe(0);
		const empty = io();
		await runCli(["devices"], env, empty);
		expect(empty.stdout).toContain("no paired browsers");
	});

	test("check passes for the live relay; status reports it; doctor has no failure for a healthy setup", async () => {
		relay.repair();
		const check = io();
		expect(await runCli(["check"], env, check)).toBe(0);
		expect(check.stdout.trim()).toBe("ok");
		const status = io();
		expect(await runCli(["status", "--json"], env, status)).toBe(0);
		const report = JSON.parse(status.stdout);
		expect(report.relay.running).toBe(true);
		expect(report.adapter).toBe("token");
		expect(report.ompSettingsToUse[0]).toBe(`omp config set collab.relayUrl ws://127.0.0.1:${relay.hostPort}`);
		expect(JSON.stringify(report)).not.toContain(relay.cookie);
		const doctor = io();
		await runCli(["doctor", "--json"], env, doctor);
		const findings = JSON.parse(doctor.stdout) as Array<{ id: string; level: string; message: string }>;
		const failures = findings.filter(f => f.level === "fail").map(f => `${f.id}: ${f.message}`);
		// the scratch web directory is not an installed build: that is the one thing this setup lacks
		expect(failures.filter(f => !f.startsWith("web:"))).toEqual([]);
		expect(findings.find(f => f.id === "host-port")?.level).toBe("ok");
		expect(findings.find(f => f.id === "guest-port")?.level).toBe("ok");
	});

	test("doctor warns when omp points elsewhere, with the exact command to fix it", async () => {
		const other = writeFakeOmp(path.join(relay.root, "other"), { relayUrl: "wss://relay.example.invalid" });
		const doctor = io();
		await runCli(["doctor", "--json"], { ...env, OMP_BIN: other.bin }, doctor);
		const f = (JSON.parse(doctor.stdout) as Array<{ id: string; level: string; fix?: string }>).find(x => x.id === "omp-settings");
		expect(f?.level).toBe("warn");
		expect(f?.fix).toBe(`omp config set collab.relayUrl ws://127.0.0.1:${relay.hostPort}`);
	});

	test("dropins prints both documents and writes nothing; --write writes them (from a config file, with no stray overrides)", async () => {
		const configFile = path.join(relay.root, "dropin-config.json");
		fs.writeFileSync(configFile, JSON.stringify({ schemaVersion: 1, listen: { hostPort: relay.hostPort, guestPort: relay.guestPort } }), { mode: 0o600 });
		const clean = { HOME: relay.root, XDG_CONFIG_HOME: path.join(relay.root, "xdg-config"), OMP_BIN: env.OMP_BIN as string, PATH: "/usr/bin:/bin", OMP_PRIVATE_RELAY_CONFIG: configFile };
		const out = io();
		expect(await runCli(["dropins"], clean, out)).toBe(0);
		expect(out.stdout).toContain('"protocol": "services/1"');
		expect(out.stdout).toContain('"protocol": "health/1"');
		expect(out.stdout).toContain("Nothing written");
		expect(fs.existsSync(path.join(relay.root, "xdg-config", "omp-services"))).toBe(false);
		const written = io();
		expect(await runCli(["dropins", "--write"], clean, written)).toBe(0);
		expect(fs.existsSync(path.join(relay.root, "xdg-config", "omp-services", "health.d", "omp-private-relay.json"))).toBe(true);
	});
});

describe("when the relay is not what answers on its ports", () => {
	test("a different program on the guest port is reported by doctor and check, and the relay's key is never offered to it", async () => {
		const root = scratch();
		const [host, guest] = await freePorts(2);
		const seen: string[] = [];
		const impostor = Bun.serve({
			hostname: "127.0.0.1",
			port: guest,
			fetch(req) {
				seen.push(new URL(req.url).search);
				return new Response(JSON.stringify({ mac: "0".repeat(64) }), { headers: { "Content-Type": "application/json" } });
			},
		});
		const check = path.join(root, "check");
		fs.mkdirSync(check, { mode: 0o700 });
		fs.writeFileSync(path.join(check, "check.key"), "ab".repeat(32), { mode: 0o600 });
		const env = { HOME: root, OMP_PRIVATE_RELAY_HOST_PORT: String(host), OMP_PRIVATE_RELAY_GUEST_PORT: String(guest), OMP_PRIVATE_RELAY_CHECK_DIR: check, PATH: "/usr/bin:/bin" };
		try {
			const out = io();
			expect(await runCli(["check"], env, out)).toBe(1);
			const doctor = io();
			expect(await runCli(["doctor"], env, doctor)).toBe(1);
			expect(doctor.stdout).toContain("does not prove to be this relay");
			expect(seen.filter(Boolean).every(s => s.startsWith("?nonce="))).toBe(true);
			expect(JSON.stringify(seen)).not.toContain("abab");
		} finally {
			impostor.stop(true);
		}
	});

	test("nothing running at all: check fails, doctor says the relay is not running", async () => {
		const root = scratch();
		const [host, guest] = await freePorts(2);
		const env = { HOME: root, OMP_PRIVATE_RELAY_HOST_PORT: String(host), OMP_PRIVATE_RELAY_GUEST_PORT: String(guest), PATH: "/usr/bin:/bin" };
		expect(await runCli(["check"], env, io())).toBe(1);
		const doctor = io();
		expect(await runCli(["doctor"], env, doctor)).toBe(1);
		expect(doctor.stdout).toContain("is not answering");
	});
});
