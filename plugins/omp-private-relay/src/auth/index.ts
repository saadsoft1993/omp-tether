// Adapter registry. Adding an adapter means adding a case here, a value in the config schema and a document; the relay
// itself does not change.
import type { Config } from "../config";
import { createTailscaleAuth, lookupFromCommand } from "./tailscale-serve";
import { createTokenAuth } from "./token";
import type { GuestAuth } from "./types";

export function createAuth(config: Config): GuestAuth {
	switch (config.auth.adapter) {
		case "token":
			return createTokenAuth({ guestPort: config.listen.guestPort, allowedHosts: config.auth.token.allowedHosts });
		case "tailscale-serve":
			return createTailscaleAuth({
				guestPort: config.listen.guestPort,
				servePort: config.auth.tailscale.servePort,
				owner: config.auth.tailscale.owner,
				publicHost: config.auth.tailscale.publicHost,
				lookup: lookupFromCommand(config.auth.tailscale.bin),
			});
	}
}

export type { AdapterHooks, GuestAuth, RequestFacts, Verdict } from "./types";
