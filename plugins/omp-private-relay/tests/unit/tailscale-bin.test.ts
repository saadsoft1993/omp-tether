// Where the relay finds the `tailscale` command: the configured absolute path, else PATH, else the usual install places
// (a service or an ssh session starts with a small PATH that lacks /usr/local/bin), and a message that names the config key
// when it is nowhere.
import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { runDoctor } from "../../src/doctor";
import { findTailscale, TAILSCALE_PLACES } from "../../src/runtime";

const dirs: string[] = [];
const scratch = (): string => {
	const d = fs.mkdtempSync(path.join(os.tmpdir(), "ts-bin-"));
	dirs.push(d);
	return d;
};
afterEach(() => {
	for (const d of dirs.splice(0)) fs.rmSync(d, { recursive: true, force: true });
});
const fakeBin = (dir: string, name: string): string => {
	fs.mkdirSync(dir, { recursive: true });
	const file = path.join(dir, name);
	fs.writeFileSync(file, "#!/bin/sh\nexit 0\n", { mode: 0o755 });
	return file;
};

describe("finding the tailscale command", () => {
	test("the usual places are the Homebrew and package locations and the macOS app bundle", () => {
		expect(TAILSCALE_PLACES).toContain("/usr/local/bin/tailscale");
		expect(TAILSCALE_PLACES).toContain("/opt/homebrew/bin/tailscale");
		expect(TAILSCALE_PLACES).toContain("/Applications/Tailscale.app/Contents/MacOS/Tailscale");
	});

	test("an absolute configured path wins, even when PATH has another tailscale", () => {
		const root = scratch();
		const configured = fakeBin(path.join(root, "chosen"), "tailscale");
		fakeBin(path.join(root, "onpath"), "tailscale");
		expect(findTailscale(configured, { PATH: path.join(root, "onpath") }, [])).toBe(configured);
	});

	test("a configured path that does not exist is not replaced by a search: the user named it", () => {
		const root = scratch();
		fakeBin(path.join(root, "onpath"), "tailscale");
		expect(findTailscale(path.join(root, "gone", "tailscale"), { PATH: path.join(root, "onpath") }, [])).toBeNull();
	});

	test("the bare name is looked up on PATH first", () => {
		const root = scratch();
		const onPath = fakeBin(path.join(root, "onpath"), "tailscale");
		const place = fakeBin(path.join(root, "place"), "tailscale");
		expect(findTailscale("tailscale", { PATH: path.join(root, "onpath") }, [place])).toBe(onPath);
	});

	test("with a small PATH the usual places are tried in order", () => {
		const root = scratch();
		const first = fakeBin(path.join(root, "a"), "tailscale");
		const second = fakeBin(path.join(root, "b"), "tailscale");
		expect(findTailscale("tailscale", { PATH: "/usr/bin:/bin" }, [path.join(root, "missing"), first, second])).toBe(first);
	});

	test("a directory or a file that is not executable is not a tailscale", () => {
		const root = scratch();
		fs.mkdirSync(path.join(root, "dir", "tailscale"), { recursive: true });
		const plain = path.join(root, "plain");
		fs.writeFileSync(plain, "x", { mode: 0o644 });
		expect(findTailscale("tailscale", { PATH: "" }, [path.join(root, "dir", "tailscale"), plain])).toBeNull();
	});

	test("nowhere: null", () => {
		expect(findTailscale("tailscale", { PATH: "" }, [])).toBeNull();
	});
});

describe("doctor without a tailscale command", () => {
	test("names the config key and the environment variable, and the places it looked", async () => {
		const root = scratch();
		const findings = await runDoctor({ HOME: root, PATH: "/usr/bin:/bin", OMP_PRIVATE_RELAY_AUTH: "tailscale-serve", OMP_PRIVATE_RELAY_TAILSCALE_BIN: path.join(root, "nowhere", "tailscale") });
		const f = findings.find(x => x.id === "tailscale");
		expect(f?.level).toBe("fail");
		expect(f?.message).toContain(path.join(root, "nowhere", "tailscale"));
		expect(f?.fix).toContain("auth.tailscale.bin");
		expect(f?.fix).toContain("OMP_PRIVATE_RELAY_TAILSCALE_BIN");
	});
});
