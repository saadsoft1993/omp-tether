import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as path from "node:path";
import { gunzipSync } from "node:zlib";
import { GzipCache, MAX_GZIP_ASSET_BYTES, MAX_GZIP_CACHE_BYTES } from "../../src/gzip-cache";
import { guestHeaders, rawRequest, startRelay, type Relay } from "./support/harness";

describe("static compression", () => {
	let relay: Relay;
	let disabled: Relay;
	beforeAll(async () => {
		relay = await startRelay({ adapter: "token" });
		disabled = await startRelay({ adapter: "token", env: { OMP_PRIVATE_RELAY_WEB_COMPRESSION: "false" } });
	});
	afterAll(async () => { await relay.stop(); await disabled.stop(); });

	test("static build assets negotiate gzip without changing security and cache headers", async () => {
		const url = "/abcd1234.js";
		const gzip = await rawRequest(relay.guestPort, url, guestHeaders(relay, { "Accept-Encoding": "br, gzip" }));
		const identity = await rawRequest(relay.guestPort, url, guestHeaders(relay, { "Accept-Encoding": "gzip;q=0, *;q=1" }));
		expect(gzip.headers["content-encoding"]).toBe("gzip");
		expect(gzip.headers.vary).toBe("Accept-Encoding");
		expect(identity.headers.vary).toBe("Accept-Encoding");
		expect(identity.headers["content-encoding"]).toBeUndefined();
		expect(gunzipSync(Buffer.from(gzip.body, "latin1")).toString()).toBe(identity.body);
		expect(gzip.headers["content-security-policy"]).toBe(identity.headers["content-security-policy"]);
		expect(gzip.headers["cache-control"]).toBe(identity.headers["cache-control"]);
		const pair = await rawRequest(relay.guestPort, "/pair", guestHeaders(relay, { "Accept-Encoding": "gzip" }));
		expect(pair.headers["content-encoding"]).toBeUndefined();
		const off = await rawRequest(disabled.guestPort, url, guestHeaders(disabled, { "Accept-Encoding": "gzip" }));
		expect(off.headers["content-encoding"]).toBeUndefined();
	});

	test("negotiates identity and wildcard preferences", async () => {
		for (const [encoding, expected] of [
			["gzip;q=0.1, identity;q=1", undefined],
			["gzip;q=0.8, identity;q=0.3", "gzip"],
			["gzip;q=0, *;q=0", undefined],
			["gzip;q=0.5, identity;q=0.5", "gzip"],
			["*;q=1, identity;q=0", "gzip"],
		] as const) {
			const response = await rawRequest(relay.guestPort, "/abcd1234.js", guestHeaders(relay, { "Accept-Encoding": encoding }));
			const got = response.headers["content-encoding"];
			if (expected === undefined) expect(got).toBeUndefined();
			else expect(got).toBe(expected);
		}
	});

	test("large files remain identity encoded", async () => {
		fs.writeFileSync(path.join(relay.webDir, "large.js"), Buffer.alloc(MAX_GZIP_ASSET_BYTES + 1, 65));
		const response = await rawRequest(relay.guestPort, "/large.js", guestHeaders(relay, { "Accept-Encoding": "gzip" }));
		expect(response.headers["content-encoding"]).toBeUndefined();
		expect(response.body.length).toBe(MAX_GZIP_ASSET_BYTES + 1);
	});

	test("a rebuilt client serves the replacement asset", async () => {
		const headers = guestHeaders(relay, { "Accept-Encoding": "gzip" });
		const first = await rawRequest(relay.guestPort, "/abcd1234.js", headers);
		expect(gunzipSync(Buffer.from(first.body, "latin1")).toString()).toBe("console.log(1)");
		fs.writeFileSync(path.join(relay.webDir, "abcd1234.js"), "console.log('new build')");
		fs.writeFileSync(path.join(relay.webDir, ".omp-private-relay-build.json"), JSON.stringify({ treeSha256: "new-build" }));
		const second = await rawRequest(relay.guestPort, "/abcd1234.js", headers);
		expect(gunzipSync(Buffer.from(second.body, "latin1")).toString()).toBe("console.log('new build')");
	});

	test("a new build generation clears old cache entries and total bytes stay bounded", () => {
		const cache = new GzipCache();
		const chunk = new Uint8Array(MAX_GZIP_ASSET_BYTES);
		for (let i = 0; i < 5; i++) cache.get("build-one", `file-${i}`, "v1", () => chunk);
		expect(cache.byteLength).toBe(MAX_GZIP_CACHE_BYTES);
		expect(cache.count).toBe(4);
		cache.get("build-two", "new-file", "v1", () => new Uint8Array(9));
		expect(cache.byteLength).toBe(9);
		expect(cache.count).toBe(1);
		let loaded = false;
		cache.get("build-two", "file-4", "v1", () => { loaded = true; return new Uint8Array(1); });
		expect(loaded).toBe(true);
	});
});
