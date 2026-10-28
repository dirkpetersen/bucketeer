import { h, append } from "../dom.js";
import { icon } from "../icons.js";
import { createSetupLink } from "../api.js";
import { copyText } from "../clipboard.js";
import { toast } from "./toast.js";
import { zipOne } from "../zip.js";

// "Set up this computer": the user makes a one-time command, pastes it in a terminal, and the
// server answers with a script that installs the AWS profile. No secret is shown or fetched
// here. The link (a bearer token) lives only in this closure and in the visible command text:
// it is dropped when it expires, when the OS tab changes, or when the panel leaves the page.
const OSES = [["windows", "Windows"], ["macos", "macOS"], ["linux", "Linux"]];
const SHELL = { windows: "PowerShell", macos: "Terminal", linux: "Terminal" };
const HOW = {
  windows: "Run the file. If Windows SmartScreen asks, click More info, then Run anyway.",
  macos: "Open the zip, then double-click bucketeer-setup.command. If macOS blocks it, open System Settings > Privacy & Security and click Open Anyway, or paste the command instead.",
  linux: "Then run: sh ~/Downloads/bucketeer-setup.sh",
};

export function detectOs() {
  const p = (navigator.userAgentData?.platform || navigator.platform || navigator.userAgent || "").toLowerCase();
  if (p.includes("mac") || p.includes("iphone") || p.includes("ipad")) return "macos";
  if (p.includes("win")) return "windows";
  return "linux";
}

const clock = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;

function saveFile(name, bytes, type) {
  const url = URL.createObjectURL(new Blob([bytes], { type }));
  const a = h("a", { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// The file holds only the token URL, never a secret.
function downloadFor(os, link) {
  const enc = (s) => new TextEncoder().encode(s);
  const sh = `#!/bin/sh\n${link.command}\n`;
  if (os === "windows") {
    const cmd = `@echo off\r\npowershell -NoProfile -ExecutionPolicy Bypass -Command "${link.command}"\r\npause\r\n`;
    return saveFile("bucketeer-setup.cmd", enc(cmd), "application/octet-stream");
  }
  if (os === "macos") return saveFile("bucketeer-setup.zip", zipOne("bucketeer-setup.command", enc(sh), 0o755), "application/zip");
  return saveFile("bucketeer-setup.sh", enc(sh), "text/x-shellscript");
}

export function setupPanel({ endpointUrl, profile = "bucketeer" }) {
  let os = detectOs();
  let link = null;
  let deadline = 0;
  let timer = 0;
  let busy = false;

  const stop = () => { clearInterval(timer); timer = 0; link = null; };
  const root = h("section", { class: "sp", "aria-labelledby": "sp-title" });
  const body = h("div", { class: "sp-body" });
  const tabs = h("div", { class: "sp-os", role: "radiogroup", "aria-label": "Operating system" });

  const paintTabs = () => tabs.replaceChildren(...OSES.map(([id, name]) => {
    const b = h("button", { class: "sp-os-btn", type: "button", role: "radio", "aria-checked": String(id === os), text: name });
    b.addEventListener("click", () => { if (id !== os) { os = id; stop(); paintTabs(); paintIdle(); } });
    return b;
  }));

  const create = async (btn) => {
    if (busy) return;
    busy = true;
    btn.disabled = true;
    try {
      const mine = os;
      const r = await createSetupLink(mine);
      if (mine !== os) return; // the tab changed while waiting
      link = r;
      deadline = performance.now() + r.expiresIn * 1000;
      paintLink();
      timer = setInterval(tick, 1000);
    } catch (err) {
      if (!(err.name === "ApiError" && err.status === 401)) toast.error(err.message || "Could not create the command. Try again.");
      btn.disabled = false;
    } finally {
      busy = false;
    }
  };

  const paintIdle = () => {
    const btn = h("button", { class: "btn btn-primary", type: "button", disabled: !endpointUrl }, icon("key"), "Create setup command");
    btn.addEventListener("click", () => create(btn));
    body.replaceChildren(btn);
  };

  const timerEl = h("p", { class: "sp-timer" });
  const tick = () => {
    if (!root.isConnected) return stop();
    const left = Math.ceil((deadline - performance.now()) / 1000);
    if (left > 0) { timerEl.textContent = `Works once. Expires in ${clock(left)}`; return; }
    stop();
    const again = h("button", { class: "btn btn-primary", type: "button" }, icon("refresh"), "Create a new command");
    again.addEventListener("click", () => create(again));
    body.replaceChildren(h("p", { class: "sp-timer", role: "status", text: "Expired. The command no longer works." }), again);
  };

  const paintLink = () => {
    const cmd = link.command;
    const copy = h("button", { class: "btn btn-ghost btn-icon", type: "button", "aria-label": "Copy setup command" }, icon("copy"));
    copy.addEventListener("click", async () => {
      if (!(await copyText(cmd))) return toast.error("Could not copy. Select the text and copy it manually.");
      toast.show("Copied");
    });
    const dl = h("button", { class: "btn", type: "button" }, icon("download"), "Download setup file instead");
    dl.addEventListener("click", () => link && downloadFor(os, link));
    timerEl.textContent = `Works once. Expires in ${clock(link.expiresIn)}`;
    body.replaceChildren(
      h("p", { class: "acct-sub", text: `Open ${SHELL[os]} and paste this command.` }),
      h("div", { class: "cred-cmd sp-cmd" }, h("code", { text: cmd }), copy),
      timerEl,
      h("div", { class: "sp-alt" }, dl, h("span", { class: "acct-sub", text: HOW[os] })));
  };

  append(root, [
    h("h3", { id: "sp-title", class: "sp-title", text: "Set up this computer" }),
    h("p", { class: "acct-sub" }, "Installs your keys as the AWS profile ", h("code", { text: profile }),
      " on this computer. No secret is shown here."),
    !endpointUrl && h("p", { class: "cred-warn", role: "status", text: "The S3 endpoint is not configured. Ask an administrator to set S3_ENDPOINT_URL." }),
    tabs, body]);
  paintTabs();
  paintIdle();
  return root;
}
