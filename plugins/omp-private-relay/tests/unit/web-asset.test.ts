// The web client asset: archive format, content hash, verification before extraction, installation, download rules.
// Every archive here is built at run time; hostile ones are assembled byte by byte.
import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import {
	BUILD_META_FILE,
	checkAssetUrl,
	download,
	inspectInstalled,
	installTree,
	loadWebBuilds,
	MAX_FILES,
	packTar,
	readTree,
	sha256,
	treeHash,
	unpackTar,
	verifyAsset,
	type WebBuild,
	WebAssetError,
} from "../../src/web-asset";

const roots: string[] = [];
const tmp = (): string => {
	const r = fs.mkdtempSync(path.join(os.tmpdir(), "oprw-"));
	roots.push(r);
	return r;
};
afterEach(() => {
	for (const r of roots.splice(0)) fs.rmSync(r, { recursive: true, force: true });
});

const sample = (): Map<string, Buffer> =>
	new Map([
		["index.html", Buffer.from("<!doctype html><title>x</title>")],
		["abcd1234.js", Buffer.from("console.log(1)")],
		["NOTICE", Buffer.alloc(1500, 65)],
		["empty.txt", Buffer.alloc(0)],
	]);

function buildFor(files: Map<string, Buffer>): { tar: Buffer; build: WebBuild } {
	const tar = packTar(files);
	return {
		tar,
		build: { ompTag: "v1.2.3", bun: "1.4.4", asset: "x.tar", sha256: sha256(tar), treeSha256: treeHash(files), noticeSha256: sha256(files.get("NOTICE") as Buffer), bytes: tar.byteLength, files: files.size },
	};
}

/** A tar header written by hand: lets a test produce entries our own writer refuses to make. */
function rawEntry(name: string, body: Buffer, opts: { type?: string; prefix?: string; size?: number; badChecksum?: boolean } = {}): Buffer {
	const header = Buffer.alloc(512);
	header.write(name, 0, "latin1");
	header.write("0000644\0", 100, "latin1");
	header.write("0000000\0", 108, "latin1");
	header.write("0000000\0", 116, "latin1");
	header.write(`${(opts.size ?? body.length).toString(8).padStart(11, "0")}\0`, 124, "latin1");
	header.write("00000000000\0", 136, "latin1");
	header.write("        ", 148, "latin1");
	header.write(opts.type ?? "0", 156, "latin1");
	header.write("ustar\0", 257, "latin1");
	header.write("00", 263, "latin1");
	if (opts.prefix) header.write(opts.prefix, 345, "latin1");
	let sum = 0;
	for (const b of header) sum += b;
	header.write(`${(opts.badChecksum ? sum + 1 : sum).toString(8).padStart(6, "0")}\0 `, 148, "latin1");
	return Buffer.concat([header, body, Buffer.alloc((512 - (body.length % 512)) % 512)]);
}
const END = Buffer.alloc(1024);

describe("archive", () => {
	test("round trip, and byte-for-byte determinism whatever the insertion order", () => {
		const a = sample();
		const b = new Map([...a].reverse());
		const tar = packTar(a);
		expect(tar.equals(packTar(b))).toBe(true);
		const back = unpackTar(tar);
		expect([...back.keys()].sort()).toEqual([...a.keys()].sort());
		for (const [k, v] of a) expect(back.get(k)?.equals(v)).toBe(true);
		expect(tar.length % 512).toBe(0);
	});

	test("the system tar can list and extract it, and our reader can read an archive the system tar writes", () => {
		const files = sample();
		const root = tmp();
		fs.writeFileSync(path.join(root, "a.tar"), packTar(files));
		const listing = Bun.spawnSync(["tar", "-tf", path.join(root, "a.tar")]).stdout.toString().trim().split("\n").sort();
		expect(listing).toEqual([...files.keys()].sort());
		fs.mkdirSync(path.join(root, "x"));
		expect(Bun.spawnSync(["tar", "-xf", path.join(root, "a.tar"), "-C", path.join(root, "x")]).exitCode).toBe(0);
		expect(fs.readFileSync(path.join(root, "x", "NOTICE")).equals(files.get("NOTICE") as Buffer)).toBe(true);
		// and back: an archive made by tar itself (ustar, plain names)
		fs.mkdirSync(path.join(root, "src"));
		for (const [k, v] of files) fs.writeFileSync(path.join(root, "src", k), v);
		const made = Bun.spawnSync(["tar", "--format=ustar", "-cf", path.join(root, "b.tar"), "-C", path.join(root, "src"), ...files.keys()]);
		expect(made.exitCode).toBe(0);
		const read = unpackTar(fs.readFileSync(path.join(root, "b.tar")));
		expect(treeHash(read)).toBe(treeHash(files));
	});

	const hostile: Array<[string, () => Buffer, RegExp]> = [
		["a symbolic link", () => Buffer.concat([rawEntry("link", Buffer.alloc(0), { type: "2" }), END]), /not a regular file/],
		["a directory entry", () => Buffer.concat([rawEntry("dir", Buffer.alloc(0), { type: "5" }), END]), /not a regular file/],
		["a device", () => Buffer.concat([rawEntry("dev", Buffer.alloc(0), { type: "3" }), END]), /not a regular file/],
		["a pax extended header", () => Buffer.concat([rawEntry("pax", Buffer.from("x"), { type: "x" }), END]), /not a regular file/],
		["a GNU long-name record", () => Buffer.concat([rawEntry("long", Buffer.from("x"), { type: "L" }), END]), /not a regular file/],
		["an absolute path", () => Buffer.concat([rawEntry("/etc/passwd", Buffer.from("x")), END]), /name not allowed/],
		["a parent-directory path", () => Buffer.concat([rawEntry("a/../../x", Buffer.from("x")), END]), /name not allowed/],
		["a leading dot-dot", () => Buffer.concat([rawEntry("../x", Buffer.from("x")), END]), /name not allowed/],
		["a dot file", () => Buffer.concat([rawEntry(".hidden", Buffer.from("x")), END]), /name not allowed/],
		["a dot-file segment", () => Buffer.concat([rawEntry("a/.b", Buffer.from("x")), END]), /name not allowed/],
		["a backslash name", () => Buffer.concat([rawEntry("a\\b", Buffer.from("x")), END]), /name not allowed/],
		["a path prefix field", () => Buffer.concat([rawEntry("x", Buffer.from("x"), { prefix: "pre" }), END]), /prefix/],
		["the same name twice", () => Buffer.concat([rawEntry("a", Buffer.from("1")), rawEntry("a", Buffer.from("2")), END]), /twice/],
		["a bad header checksum", () => Buffer.concat([rawEntry("a", Buffer.from("1"), { badChecksum: true }), END]), /checksum/],
		["a declared size beyond the data", () => Buffer.concat([rawEntry("a", Buffer.from("1"), { size: 99999 }), END]), /truncated/],
		["a missing end marker", () => rawEntry("a", Buffer.from("1")), /end marker/],
		["data after the end marker", () => Buffer.concat([rawEntry("a", Buffer.from("1")), END, Buffer.from("junk")]), /after the end/],
		["no files at all", () => END, /no files/],
	];
	for (const [name, make, message] of hostile) {
		test(`refused: ${name}`, () => {
			expect(() => unpackTar(make())).toThrow(message);
		});
	}

	test("refused: more files or bytes than the limits allow", () => {
		const many = Buffer.concat([...Array.from({ length: 4 }, (_, i) => rawEntry(`f${i}`, Buffer.from("x"))), END]);
		expect(() => unpackTar(many, { files: 3, bytes: 1000 })).toThrow(/larger than allowed/);
		const big = Buffer.concat([rawEntry("f", Buffer.alloc(600)), END]);
		expect(() => unpackTar(big, { files: 10, bytes: 500 })).toThrow(/larger than allowed/);
		expect(MAX_FILES).toBeGreaterThan(30);
	});

	test("the writer refuses names the reader would refuse", () => {
		for (const bad of ["../x", "/x", ".x", "a b", "a/../b", "x".repeat(101)]) expect(() => packTar(new Map([[bad, Buffer.from("x")]]))).toThrow(WebAssetError);
	});
});

describe("content hash", () => {
	test("does not depend on order, changes with any byte or name", () => {
		const a = sample();
		const h = treeHash(a);
		expect(treeHash(new Map([...a].reverse()))).toBe(h);
		const changed = new Map(a);
		changed.set("abcd1234.js", Buffer.from("console.log(2)"));
		expect(treeHash(changed)).not.toBe(h);
		const renamed = new Map(a);
		renamed.set("abcd1235.js", renamed.get("abcd1234.js") as Buffer);
		renamed.delete("abcd1234.js");
		expect(treeHash(renamed)).not.toBe(h);
		expect(h).toMatch(/^[0-9a-f]{64}$/);
	});

	test("readTree skips dot files and refuses links", () => {
		const root = tmp();
		fs.writeFileSync(path.join(root, "index.html"), "x");
		fs.writeFileSync(path.join(root, ".omp-private-relay-build.json"), "{}");
		expect([...readTree(root).keys()]).toEqual(["index.html"]);
		fs.symlinkSync("/etc/hosts", path.join(root, "link"));
		expect(() => readTree(root)).toThrow(/not a regular file/);
	});
});

describe("verify and install", () => {
	test("an asset with the wrong sha256 is refused before anything is unpacked, and so is a wrong tree hash", () => {
		const { tar, build } = buildFor(sample());
		expect([...verifyAsset(tar, build).keys()].length).toBe(4);
		expect(() => verifyAsset(tar, { ...build, sha256: "0".repeat(64) })).toThrow(/sha256/);
		expect(() => verifyAsset(tar, { ...build, treeSha256: "0".repeat(64) })).toThrow(/tree hash/);
		const flipped = Buffer.from(tar);
		flipped[600] = (flipped[600] as number) ^ 1;
		expect(() => verifyAsset(flipped, build)).toThrow(/sha256/);
	});

	test("an archive whose hashes were pinned for other content is refused even if it is a valid archive", () => {
		const a = buildFor(sample());
		const other = new Map(sample());
		other.set("index.html", Buffer.from("<script>evil()</script>"));
		const b = buildFor(other);
		expect(() => verifyAsset(b.tar, { ...b.build, sha256: a.build.sha256 })).toThrow(/sha256/);
		expect(() => verifyAsset(b.tar, { ...b.build, treeSha256: a.build.treeSha256 })).toThrow(/tree hash/);
	});

	test("install writes private files, records the build, and replaces an older version atomically", () => {
		const root = tmp();
		const target = path.join(root, "data", "web", "current");
		const first = buildFor(sample());
		installTree(verifyAsset(first.tar, first.build), first.build, target);
		expect(fs.statSync(target).mode & 0o777).toBe(0o700);
		expect(fs.statSync(path.join(target, "index.html")).mode & 0o777).toBe(0o600);
		expect(inspectInstalled(target)).toMatchObject({ state: "ok", meta: { ompTag: "v1.2.3", treeSha256: first.build.treeSha256 } });
		const second = new Map(sample());
		second.set("abcd1234.js", Buffer.from("console.log(2)"));
		const b = buildFor(second);
		installTree(verifyAsset(b.tar, b.build), b.build, target);
		expect(fs.readFileSync(path.join(target, "abcd1234.js"), "utf8")).toBe("console.log(2)");
		expect(fs.readdirSync(path.dirname(target)).sort()).toEqual([".current.install-lock", "current"]); // the lock file stays (it is never recreated), nothing else
	});

	test("a bad name leaves the installed version untouched and writes nothing outside", () => {
		const root = tmp();
		const target = path.join(root, "web", "current");
		const good = buildFor(sample());
		installTree(sample(), good.build, target);
		const evil = new Map(sample());
		evil.set("../escape.txt", Buffer.from("x"));
		expect(() => installTree(evil, good.build, target)).toThrow(/not allowed/);
		expect(fs.existsSync(path.join(root, "web", "escape.txt"))).toBe(false);
		expect(inspectInstalled(target).state).toBe("ok");
		expect(fs.readdirSync(path.join(root, "web")).sort()).toEqual([".current.install-lock", "current"]);
	});

	test("a symlink in place of the install directory is refused", () => {
		const root = tmp();
		fs.mkdirSync(path.join(root, "elsewhere"));
		fs.mkdirSync(path.join(root, "web"), { mode: 0o700 }); // explicit: a umask of 002 would make it group-writable, which is refused
		fs.symlinkSync(path.join(root, "elsewhere"), path.join(root, "web", "current"));
		const { build } = buildFor(sample());
		expect(() => installTree(sample(), build, path.join(root, "web", "current"))).toThrow(/symlink/);
	});

	test("inspect: missing, unmanaged, ok, and modified after an edit or a stray file", () => {
		const root = tmp();
		const target = path.join(root, "web");
		expect(inspectInstalled(target).state).toBe("missing");
		fs.mkdirSync(target);
		fs.writeFileSync(path.join(target, "index.html"), "x");
		expect(inspectInstalled(target).state).toBe("unmanaged");
		const { build } = buildFor(sample());
		installTree(sample(), build, target);
		expect(inspectInstalled(target).state).toBe("ok");
		fs.appendFileSync(path.join(target, "abcd1234.js"), "//edited");
		expect(inspectInstalled(target).state).toBe("modified");
		fs.writeFileSync(path.join(target, "abcd1234.js"), "console.log(1)");
		expect(inspectInstalled(target).state).toBe("ok");
		fs.writeFileSync(path.join(target, "extra.js"), "x");
		expect(inspectInstalled(target).state).toBe("modified");
		expect(fs.existsSync(path.join(target, BUILD_META_FILE))).toBe(true);
	});
});

describe("download", () => {
	test("only https, or http to loopback; no credentials", () => {
		for (const ok of ["https://github.com/o/r/releases/download/t/a.tar", "http://127.0.0.1:1/a.tar", "http://localhost:1/a"]) expect(() => checkAssetUrl(ok)).not.toThrow();
		for (const bad of ["http://example.com/a.tar", "ftp://example.com/a", "file:///etc/passwd", "https://user:pw@example.com/a", "not a url", "http://127.0.0.1.example.com/a"]) {
			expect(() => checkAssetUrl(bad)).toThrow(WebAssetError);
		}
	});

	test("downloads from a loopback server, refuses an error status, and refuses a redirect that leaves loopback http", async () => {
		const server = Bun.serve({
			hostname: "127.0.0.1",
			port: 0,
			fetch(req) {
				const p = new URL(req.url).pathname;
				if (p === "/ok") return new Response("payload");
				if (p === "/missing") return new Response("no", { status: 404 });
				if (p === "/redirect") return Response.redirect("http://example.invalid/elsewhere", 302);
				if (p === "/hop") return Response.redirect(new URL("/ok", req.url).toString(), 302);
				if (p === "/loop") return Response.redirect(new URL("/loop", req.url).toString(), 302);
				return new Response("big", { headers: { "Content-Length": "1" } });
			},
		});
		try {
			const base = `http://127.0.0.1:${server.port}`;
			expect((await download(`${base}/ok`)).toString()).toBe("payload");
			await expect(download(`${base}/missing`)).rejects.toThrow(/HTTP 404/);
			// a redirect to a host that is neither https nor loopback is refused before any connection is made to it
			await expect(download(`${base}/redirect`)).rejects.toThrow(/only downloaded over https/);
			expect((await download(`${base}/hop`)).toString()).toBe("payload");
			await expect(download(`${base}/loop`)).rejects.toThrow(/too many redirects/);
		} finally {
			server.stop(true);
		}
	});

	test("a response larger than the limit is refused, by its declared length and by what actually arrives", async () => {
		const declared = async () => new Response("x", { headers: { "Content-Length": String(65 * 1024 * 1024) } });
		await expect(download("https://example.com/a", declared)).rejects.toThrow(/larger than allowed/);
		const chunk = new Uint8Array(1024 * 1024);
		const stream = new ReadableStream({
			pull(controller) {
				controller.enqueue(chunk);
			},
		});
		await expect(download("https://example.com/a", async () => new Response(stream))).rejects.toThrow(/larger than allowed/);
	});
});

describe("web-builds.json", () => {
	test("is well formed: hashes, names and counts", () => {
		const builds = loadWebBuilds();
		expect(builds.schema).toBe(1);
		expect(builds.repository).toMatch(/^[\w.-]+\/[\w.-]+$/);
		expect(builds.releaseTag).toMatch(/^omp-private-relay-v\d+\.\d+\.\d+$/);
		expect(Object.keys(builds.builds).length).toBeGreaterThan(0);
		for (const [minor, b] of Object.entries(builds.builds)) {
			expect(minor).toMatch(/^\d+\.\d+$/);
			expect(b.ompTag.startsWith(`v${minor}.`)).toBe(true);
			expect(b.asset).toBe(`omp-private-relay-web-${minor}.tar`);
			expect(b.sha256).toMatch(/^[0-9a-f]{64}$/);
			expect(b.treeSha256).toMatch(/^[0-9a-f]{64}$/);
			expect(b.bytes).toBeGreaterThan(1024);
			expect(b.files).toBeGreaterThan(1);
		}
	});

	test("with OMP_PRIVATE_RELAY_TEST_ASSET set, that file is the pinned asset byte for byte", () => {
		const file = process.env.OMP_PRIVATE_RELAY_TEST_ASSET;
		// `make release-check` sets the asset and requires it: a release cannot pass without the exact archive being checked
		if (!file && process.env.OMP_PRIVATE_RELAY_REQUIRE_ASSET) throw new Error("the release check needs OMP_PRIVATE_RELAY_TEST_ASSET (the archive to be published)");
		if (!file) return;
		const data = fs.readFileSync(file);
		const build = loadWebBuilds().builds["18.18"] as WebBuild;
		expect(sha256(data)).toBe(build.sha256);
		expect(treeHash(verifyAsset(data, build))).toBe(build.treeSha256);
	});
});
