// Who is listening on a port: parsers on captured output of both platforms, the ownership decision with stand-in
// system calls (macOS and Linux paths both run on every machine), and one live lookup of a real listener.
import { describe, expect, test } from "bun:test";
import * as net from "node:net";
import { isLoopbackAddress, listenersOnPort, ownership, type PlatformDeps, parseLsof, parseProcNetTcp, parseSs } from "../../src/platform";

const PROC_HEAD = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n";
const row = (addr: string, state: string, uid: number) => `   0: ${addr} 00000000:0000 ${state} 00000000:00000000 00:00000000 00000000  ${uid}        0 12345 1 0000000000000000 100 0 0 10 0\n`;

describe("parsers", () => {
	test("lsof -F pun: one record per process, one n line per socket", () => {
		const out = "p123\nu501\nn127.0.0.1:7796\nn[::1]:7796\np456\nu0\nn*:7796\n";
		expect(parseLsof(out)).toEqual([
			{ address: "127.0.0.1", port: 7796, uid: 501, pid: 123 },
			{ address: "[::1]", port: 7796, uid: 501, pid: 123 },
			{ address: "*", port: 7796, uid: 0, pid: 456 },
		]);
		expect(parseLsof("")).toEqual([]);
		expect(parseLsof("pxyz\nuabc\nnnot-a-socket\n")).toEqual([]);
	});

	test("/proc/net/tcp: little-endian addresses, hex ports, listening sockets only, the uid column", () => {
		const text = PROC_HEAD + row("0100007F:1E74", "0A", 1000) + row("00000000:1F90", "0A", 0) + row("0100007F:1E75", "01", 1000);
		expect(parseProcNetTcp(text, false)).toEqual([
			{ address: "127.0.0.1", port: 7796, uid: 1000, pid: null },
			{ address: "0.0.0.0", port: 8080, uid: 0, pid: null },
		]);
	});

	test("/proc/net/tcp6: loopback, wildcard and mapped IPv4 addresses", () => {
		const text =
			PROC_HEAD +
			row("00000000000000000000000001000000:1E74", "0A", 1000) +
			row("00000000000000000000000000000000:1E74", "0A", 0) +
			row("0000000000000000FFFF00000100007F:1E74", "0A", 1001);
		expect(parseProcNetTcp(text, true).map(l => [l.address, l.uid])).toEqual([
			["::1", 1000],
			["::", 0],
			["::ffff:127.0.0.1", 1001],
		]);
	});

	test("/proc/net/tcp: damaged lines are skipped, not guessed at", () => {
		const text = PROC_HEAD + "garbage\n" + row("0100007F:ZZZZ", "0A", 1) + row("0100", "0A", 1);
		expect(parseProcNetTcp(text, false)).toEqual([]);
	});

	test("ss -Hltne: address, port, uid and process id when shown", () => {
		const out =
			"LISTEN 0      512        127.0.0.1:7796      0.0.0.0:*    uid:1000 ino:45 sk:1b cgroup:/user.slice <->\n" +
			'LISTEN 0      4096       [::]:8080           [::]:*       users:(("x",pid=77,fd=3)) uid:0 ino:9 sk:2\n' +
			"ESTAB  0      0          127.0.0.1:7796      127.0.0.1:50000\n";
		expect(parseSs(out)).toEqual([
			{ address: "127.0.0.1", port: 7796, uid: 1000, pid: null },
			{ address: "::", port: 8080, uid: 0, pid: 77 },
		]);
	});

	test("loopback addresses", () => {
		for (const a of ["127.0.0.1", "127.1.2.3", "::1", "[::1]"]) expect(isLoopbackAddress(a)).toBe(true);
		for (const a of ["0.0.0.0", "*", "::", "192.0.2.1"]) expect(isLoopbackAddress(a)).toBe(false);
	});
});

function deps(over: Partial<PlatformDeps>): PlatformDeps {
	return { platform: "linux", readFile: () => null, run: () => null, exists: () => false, ...over };
}

describe("lookup and ownership, with stand-in system calls", () => {
	const linuxFiles = (tcp: string, tcp6 = PROC_HEAD): PlatformDeps =>
		deps({ readFile: f => (f === "/proc/net/tcp" ? tcp : f === "/proc/net/tcp6" ? tcp6 : null) });

	test("Linux: our own listener, a foreign one, a wildcard listener of someone else, none", () => {
		expect(ownership(7796, 1000, linuxFiles(PROC_HEAD + row("0100007F:1E74", "0A", 1000))).state).toBe("ours");
		expect(ownership(7796, 1000, linuxFiles(PROC_HEAD + row("0100007F:1E74", "0A", 1001))).state).toBe("foreign");
		// ours on loopback, but another account also holds the wildcard address on the same port: not ours
		expect(ownership(7796, 1000, linuxFiles(PROC_HEAD + row("0100007F:1E74", "0A", 1000) + row("00000000:1E74", "0A", 1001))).state).toBe("foreign");
		expect(ownership(7796, 1000, linuxFiles(PROC_HEAD + row("0100007F:1E75", "0A", 1000))).state).toBe("none");
	});

	test("Linux without /proc/net/tcp falls back to ss; without either the owner is unknown, never assumed ours", () => {
		const ss = "LISTEN 0 512 127.0.0.1:7796 0.0.0.0:* uid:1000 ino:45 sk:1\n";
		const viaSs = deps({ exists: f => f === "/usr/bin/ss", run: argv => (argv[0] === "/usr/bin/ss" ? { code: 0, stdout: ss } : null) });
		const found = listenersOnPort(7796, viaSs);
		expect(found.ok && found.source).toBe("ss");
		expect(ownership(7796, 1000, viaSs).state).toBe("ours");
		expect(ownership(7796, 1000, deps({})).state).toBe("unknown");
		expect(ownership(7796, 1000, deps({ exists: () => true, run: () => ({ code: 2, stdout: "" }) })).state).toBe("unknown");
	});

	test("macOS: lsof output decides; exit 1 with no output means nothing listening; a crash means unknown", () => {
		const mac = (run: PlatformDeps["run"]) => deps({ platform: "darwin", exists: f => f === "/usr/sbin/lsof", run });
		expect(ownership(7796, 501, mac(() => ({ code: 0, stdout: "p1\nu501\nn127.0.0.1:7796\n" }))).state).toBe("ours");
		expect(ownership(7796, 501, mac(() => ({ code: 0, stdout: "p1\nu0\nn*:7796\n" }))).state).toBe("foreign");
		expect(ownership(7796, 501, mac(() => ({ code: 1, stdout: "" }))).state).toBe("none");
		expect(ownership(7796, 501, mac(() => ({ code: 139, stdout: "" }))).state).toBe("unknown");
		expect(ownership(7796, 501, mac(() => null)).state).toBe("unknown");
		expect(ownership(7796, 501, deps({ platform: "darwin" })).state).toBe("unknown");
	});

	test("a listener whose uid cannot be seen is unknown, and so is any other platform", () => {
		expect(ownership(7796, 1, deps({ platform: "darwin", exists: () => true, run: () => ({ code: 0, stdout: "n127.0.0.1:7796\n" }) })).state).toBe("unknown");
		expect(ownership(7796, 1, deps({ platform: "win32" })).state).toBe("unknown");
	});

	test("the macOS lookup asks lsof with a fixed argument list (no shell, any address, listening only)", () => {
		let seen: string[] = [];
		ownership(7796, 501, deps({ platform: "darwin", exists: f => f === "/usr/sbin/lsof", run: argv => ((seen = argv), { code: 1, stdout: "" }) }));
		expect(seen).toEqual(["/usr/sbin/lsof", "-nP", "-iTCP:7796", "-sTCP:LISTEN", "-Fpun"]);
	});
});

describe("a real listener on this machine", () => {
	test("is found, and is ours", async () => {
		const server = net.createServer();
		await new Promise<void>(resolve => server.listen(0, "127.0.0.1", () => resolve()));
		const port = (server.address() as net.AddressInfo).port;
		try {
			const found = ownership(port, process.getuid?.() ?? -1);
			expect(found.state).toBe("ours");
		} finally {
			server.close();
		}
	});

	test("a port nobody listens on is none", async () => {
		const server = net.createServer();
		await new Promise<void>(resolve => server.listen(0, "127.0.0.1", () => resolve()));
		const port = (server.address() as net.AddressInfo).port;
		await new Promise<void>(resolve => server.close(() => resolve()));
		expect(ownership(port, process.getuid?.() ?? -1).state).toBe("none");
	});
});
