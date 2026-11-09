import "./styles.css";
import { createRoot } from "react-dom/client";
import App from "./App";
import { redeemPairingLink } from "./data/pairing";

const root = document.getElementById("root");
if (!root) throw new Error("missing #root");
addEventListener("hashchange", () => redeemPairingLink());
if (!redeemPairingLink()) createRoot(root).render(<App />);
