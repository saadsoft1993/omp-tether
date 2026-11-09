// The pairing page and its script, served by the guest listener in `own` pairing mode. The one-time token travels in the
// URL fragment (never sent to a server, never in a log); the script removes it from the address bar before it does
// anything else, then redeems it with one POST.
export const PAIR_PAGE = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Pair this browser</title>
</head>
<body style="font:16px system-ui,sans-serif;max-width:34rem;margin:3rem auto;padding:0 1rem">
<h1>Pair this browser</h1>
<p id="m">Pairing…</p>
<script src="/__pair.js"></script>
</body>
</html>
`;

export const PAIR_JS = `(async () => {
  const m = document.getElementById("m");
  const token = new URLSearchParams(location.hash.slice(1)).get("token");
  history.replaceState(null, "", location.pathname);
  if (!token) {
    m.textContent = "This page needs the one-time link that \\"omp-private-relay pair\\" prints. Run it again and open that link.";
    return;
  }
  try {
    const r = await fetch("/__pair", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Omp-Relay": "1" },
      body: JSON.stringify({ token }),
    });
    if (r.ok) m.textContent = "Paired. You can close this tab and open a session link from omp (/collab) in this browser.";
    else if (r.status === 403) m.textContent = "That link was already used or has expired. Run \\"omp-private-relay pair\\" again.";
    else if (r.status === 429) m.textContent = "Too many attempts. Wait a minute and try again.";
    else m.textContent = "Pairing failed (" + r.status + ").";
  } catch {
    m.textContent = "Could not reach the relay.";
  }
})();
`;
