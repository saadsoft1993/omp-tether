import { KeyRound } from "lucide-react";
import { useState } from "react";
import { takePairingError } from "../data/pairing";
import { Card, ErrorState } from "../ui";
import "../routes/hub.css";

/**
 * Shown instead of the dashboard while the hub answers 401: this browser has no pairing cookie yet.
 * Pairing needs a single-use link printed on the machine the hub runs on; the page never shows or asks for a secret.
 */
export function PairScreen() {
	const [error] = useState(takePairingError);
	return (
		<main className="pair-screen">
			<Card
				title={
					<span className="row">
						<KeyRound size={15} /> Pair this browser
					</span>
				}
				description="This dashboard only answers browsers you paired with it."
			>
				{error && <ErrorState error={`Pairing failed: ${error}`} />}
				<ol className="pair-steps">
					<li>
						On the machine the hub runs on, in a terminal (not inside an omp session, or the link lands in its
						transcript), run:
						<pre className="mono">omp-dashboard pair</pre>
					</li>
					<li>
						Open the link it prints in this browser, once, within 5 minutes. From another device, send it to
						yourself some way only you can read (a password manager's secure note, an encrypted message).
					</li>
				</ol>
				<p className="micro">
					The link works once and expires after 5 minutes either way. This browser then keeps a private cookie for a
					year; <code>omp-dashboard devices</code> lists paired browsers and{" "}
					<code>omp-dashboard revoke &lt;id&gt;</code> unpairs one.
				</p>
			</Card>
		</main>
	);
}
