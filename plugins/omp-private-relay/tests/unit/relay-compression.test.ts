import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { gunzipSync } from "node:zlib";
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
});
