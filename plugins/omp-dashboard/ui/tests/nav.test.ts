import { describe, expect, it } from "bun:test";
import { sectionFromHash, usageHref, webUrl } from "../src/app/nav";

describe("navigation", () => {
	it("reads the page from the hash and falls back to Sessions", () => {
		expect(sectionFromHash("#/relay")).toBe("relay");
		expect(sectionFromHash("#/alerts?x=1")).toBe("alerts");
		for (const other of ["", "#", "#/nope", "#/__proto__", "#pair=abc"]) expect(sectionFromHash(other)).toBe("sessions");
	});

	it("links to the stats page only for an http or https address", () => {
		expect(usageHref("http://127.0.0.1:3847/")).toBe("http://127.0.0.1:3847/");
		expect(usageHref("https://stats.example.test/usage")).toBe("https://stats.example.test/usage");
		for (const bad of [null, undefined, "", "javascript:alert(1)", "data:text/html,x", "//host/x", "not a url", "file:///etc/passwd"]) {
			expect(usageHref(bad)).toBeNull();
		}
	});

	it("opens only http or https addresses from the hub's link answer", () => {
		expect(webUrl("https://box.example.test/#ws://x/r/a")).toBe("https://box.example.test/#ws://x/r/a");
		for (const bad of ["javascript:alert(1)", "data:text/html,x", "blob:https://a/b", "file:///x", "//a/b", "/relative", "", null, 5, undefined]) {
			expect(webUrl(bad)).toBeNull();
		}
	});
});
