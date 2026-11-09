// The command-line adapter that lets pairing-store-suite.ts drive this plugin's reader and writer (src/pairing-store.ts).
// It is test support, not part of the plugin's command line. See the header of pairing-store-suite.ts for the contract.
import { PairingError, PairingReader, PairingWriter } from "../../src/pairing-store";

const [command, dir, ...rest] = Bun.argv.slice(2);
const out = (value: unknown): void => console.log(JSON.stringify(value));

try {
	if (!command || !dir) throw new Error("usage: adapter.ts <command> <dir> ...");
	if (command === "check") {
		const [cookieName, header] = rest;
		const reader = new PairingReader(dir);
		const view = reader.read();
		const device = view.refusal ? null : reader.match(header ?? null, cookieName ?? "");
		out({ paired: device !== null, device, refused: view.refusal });
	} else {
		const writer = new PairingWriter(dir);
		if (command === "create-token") out({ token: await writer.createToken() });
		else if (command === "redeem") out({ session: await writer.redeem(rest[0], rest[1] ?? null) });
		else if (command === "devices") out({ devices: writer.devices().map(d => ({ id: d.id, label: d.label, pairedAt: d.pairedAt })) });
		else if (command === "revoke") out({ revoked: await writer.revoke(rest[0] === "all" ? null : (rest[0] ?? "")) });
		else throw new Error(`unknown command ${command}`);
	}
} catch (err) {
	out({ error: err instanceof PairingError ? err.message : String(err) });
	process.exit(1);
}
