import { expect, mock, test } from "bun:test";
import { Client } from "./support/client";
import { FakeHost } from "./support/fake-host";

mock.module("@oh-my-pi/pi-utils/dirs", () => ({ VERSION: "18.18.0" }));

test("omp 18.18 does not advertise or accept plan-notes", async () => {
	const host = await FakeHost.start({ enable: true, planReview: true, session: true });
	await host.fire("session_start");
	const { client, nonce, sessionId, generation } = await Client.toHost(host);
	try {
		expect(host.registry()?.data.ops).not.toContain("plan-notes");
		const reply = await client.request({ v: 1, id: 1, nonce, sessionId, generation, op: "plan-notes", args: {} });
		expect(reply.code).toBe("unsupported");
	} finally {
		client.close();
		await host.dispose();
	}
});
