import "./components/secret-field.js";
import "./components/code-panel.js";
import { toast } from "./components/toast.js";
import { confirmDialog } from "./components/confirm-dialog.js";
import "./components/menu-button.js";
import { h } from "./dom.js";
import { save } from "./storage.js";

// Demo data only; these are not real credentials.
document.getElementById("sf-demo").value = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY";

const cp = document.getElementById("cp-demo");
cp.tabs = [
  {
    label: "~/.aws/config", filename: "config",
    content: "# Managed by Bucketeer\n[profile bucketeer]\nregion = us-east-1\nendpoint_url = https://s3.example.org\ns3 =\n  addressing_style = path\n",
  },
  {
    label: "~/.aws/credentials", filename: "credentials",
    content: "[bucketeer]\naws_access_key_id = AKIAIOSFODNN7EXAMPLE\naws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n",
  },
];
cp.hint = "Save both files, then run: aws --profile bucketeer s3 ls";

const sw = document.getElementById("swatches");
for (const t of ["bg", "surface", "surface-2", "text", "text-muted", "accent", "accent-text", "border-strong", "danger", "success"]) {
  const chip = h("i");
  chip.style.background = `var(--${t})`;
  sw.append(h("div", { class: "swatch" }, chip, h("span", { text: `--${t}` })));
}

document.getElementById("t-info").addEventListener("click", () => toast.show("Access key copied"));
document.getElementById("t-err").addEventListener("click", () => toast.error("Could not reach the server"));
const result = document.getElementById("d-result");
document.getElementById("d-simple").addEventListener("click", async () => {
  const ok = await confirmDialog({ title: "Rotate access key?", message: "A new key is created first; the old one keeps working until you delete it.", confirmLabel: "Rotate key" });
  result.textContent = ok ? "Confirmed." : "Cancelled.";
});
document.getElementById("d-type").addEventListener("click", async () => {
  const ok = await confirmDialog({ title: "Delete bucket?", message: "This permanently deletes the bucket and everything in it.", confirmLabel: "Delete bucket", danger: true, requireText: "genomics-archive" });
  result.textContent = ok ? "Deleted (demo)." : "Cancelled.";
});

const themes = [null, "light", "dark"];
document.getElementById("toggle-theme").addEventListener("click", () => {
  const cur = themes.indexOf(document.documentElement.dataset.theme ?? null);
  const next = themes[(cur + 1) % 3];
  if (next) document.documentElement.dataset.theme = next;
  else delete document.documentElement.dataset.theme;
  save("theme", next);
});

const mbResult = document.getElementById("mb-result");
const pick = (name) => () => { mbResult.textContent = `Chose: ${name}`; };
for (const el of document.querySelectorAll("menu-button")) {
  el.items = [
    { label: "Keys", icon: "key", hint: "Keys that work for this bucket only", badge: 2, onSelect: pick("Keys") },
    { label: "Lifecycle", icon: "refresh", hint: "Delete old files automatically", onSelect: pick("Lifecycle") },
    { label: "Share link", icon: "download", disabled: true, reason: "Add a file first", onSelect: pick("Share link") },
    { label: "Delete bucket", icon: "trash", danger: true, separatorBefore: true, onSelect: pick("Delete") },
  ];
}
