/** The HOLD stage: each rule, its false-positive guards, and release by originKey. */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { runImport } from "../engine";
import { findHolds } from "../hold";
import { releaseList } from "../main";
import { type Env, makeEnv } from "./helpers";
import { useLongTimeouts } from "./timeout";

useLongTimeouts();

describe("rule 1: password-like word followed by a value", () => {
	test.each([
		"The admin password: Hunter2xyz for staging",
		"db passwd = s3cr3t!",
		"passphrase is Tr0ub4dor&3",
		"the api_key -> a1b2c3d4",
		"Secret: CorrectHorse",
	])("holds %s", text => expect(findHolds(text)).toContain("hold:password-value"));

	test.each([
		"We should rotate the password regularly.",
		"The password is required for login.",
		"token: refresh tokens expire quickly",
		"Read the [password](https://example.com/docs) page and the [token](https://x.io/a1) page.",
		"password: [REDACTED:assignment]",
		"password: <your-password>",
		"export PASSWORD=$DB_PASSWORD",
		"password = **required**",
	])("does not hold %s", text => expect(findHolds(text)).not.toContain("hold:password-value"));
});

describe("rule 2: user@host / value pairs", () => {
	test.each(["login: admin@dev-tenant.test / DevAdmin123!", "ssh root@10.0.0.5 / Zz9-pass", "user alice / Wonder1and"])(
		"holds %s",
		text => expect(findHolds(text)).toContain("hold:credential-pair"),
	);
	test.each(["admin: read / write access", "the user can choose on / off", "root dir / src layout"])("does not hold %s", text =>
		expect(findHolds(text)).not.toContain("hold:credential-pair"),
	);
});

describe("rule 3: key material, bearer tokens, prefixes, URL credentials", () => {
	// Fixtures that look like credentials are assembled from fragments, so that no source line holds one whole (a credential
	// scanner over this repository then finds nothing to report).
	const header = (kind: string): string => `${"-".repeat(5)}BEGIN ${kind}${"-".repeat(5)}`;
	test("private key headers", () => {
		expect(findHolds(`${header("OPENSSH PRIVATE" + " KEY")}\nabc`)).toEqual(["hold:private-key"]);
		expect(findHolds(header("RSA PRIVATE" + " KEY"))).toEqual(["hold:private-key"]);
		expect(findHolds(header("PUBLIC KEY"))).toEqual([]);
	});
	test("bearer tokens need 20+ characters", () => {
		expect(findHolds("Authorization: Bearer abcdefghijklmnopqrstuvwx")).toContain("hold:bearer-token");
		expect(findHolds("send a Bearer token in the header")).toEqual([]);
	});
	test.each(["sk-abcdefghijklmnop12", "ghp_abcdefghijklmnopqrstuvwx", "glpat-abcdefghij12345", "xoxb-1234567890-abcdef", "AKIA" + "ABCDEFGHIJKLMNOP"])(
		"prefix %s",
		text => expect(findHolds(`use ${text} here`)).toContain("hold:token-prefix"),
	);
	test("URLs with user:pass@", () => {
		expect(findHolds("postgres://app:S3cretPw@db.example:5432/x")).toContain("hold:url-credentials");
		expect(findHolds("scheme://user:password@host is an example")).toEqual([]);
		expect(findHolds("see https://example.com/a@b and ssh://git@host/repo")).toEqual([]);
	});
});

describe("rule 4: long mixed tokens next to key/token/secret", () => {
	test("holds", () => {
		expect(findHolds("the deploy key Qm9yZWRBcGVzQXJlQm9yaW5nMTIz is on the box")).toContain("hold:long-token");
		expect(findHolds("secret 0123456789abcdef0123456789abcdef")).toContain("hold:long-token");
	});
	test("a lowercase hyphenated slug or a short word does not", () => {
		expect(findHolds("see the api-key-management-service-configuration doc")).toEqual([]);
		expect(findHolds("the key concept of the design")).toEqual([]);
	});
});

let env: Env;
beforeAll(async () => {
	env = await makeEnv();
	const cwd = env.folder("held");
	const dir = env.project(cwd);
	env.memory(dir, "creds.md", { name: "Held creds" }, "Login: admin@dev.example / DevAdmin123! on the box.");
	env.memory(dir, "prose.md", { name: "Held prose" }, "Rotate the deploy credentials monthly and keep the password policy in mind.");
	env.emptyBank(env.bankOf(cwd));
});
afterAll(() => env.cleanup());

describe("hold and release in a run", () => {
	test("held items are listed by key and kind only; release imports exactly the named ones", async () => {
		const plan = await runImport(env.options({ command: "dry-run" }));
		expect(plan.counts.skippedHeld).toBe(1);
		expect(plan.held).toEqual([{ originKey: expect.stringMatching(/#creds$/), kinds: ["hold:credential-pair"] }]);
		expect(JSON.stringify(plan)).not.toContain("DevAdmin123");
		const key = plan.held[0]?.originKey ?? "";
		const released = await runImport(env.options({ command: "import", apply: true, releaseHeld: [key] }));
		expect(released.counts.imported).toBe(2);
		expect(released.counts.skippedHeld).toBe(0);
	});

	test("release list: commas or a file", async () => {
		expect(releaseList("a#b,c#d")).toEqual(["a#b", "c#d"]);
		const file = `${env.root}/release.txt`;
		await Bun.write(file, "# reviewed\nx#y\n\nz#w\n");
		expect(releaseList(file)).toEqual(["x#y", "z#w"]);
	});
});
