// The Unfret pass: what shoppers call an API key. A pass is `ufk_<id>_<secret>`; its ID (10 letters and digits) is
// public and names it, shown in capitals in two groups ("ABCDE-FGHIJ") so it can be read out and compared. Its
// recovery code is the "backup code". The API and the code keep the technical names; only the words change.

export const PASS_RE = /^ufk_([a-z2-7]{10})_[a-z2-7]{52}$/;
export const BACKUP_RE = /^ufr_([a-z2-7]{10})_[a-z2-7]{52}$/;
const ID_RE = /^[a-z2-7]{10}$/;

/** The ID inside a whole pass, or null. */
export function passIdOf(pass) {
  return PASS_RE.exec(String(pass ?? "").trim())?.[1] ?? null;
}

/** A pass ID as shoppers see it: "ABCDE-FGHIJ". */
export function showPassId(id) {
  const value = String(id ?? "").toUpperCase();
  return value.length === 10 ? `${value.slice(0, 5)}-${value.slice(5)}` : value;
}

/** A pass ID as typed ("ABCDE-FGHIJ", "abcdefghij", with spaces) or a whole pass, as the API's key ID; or null. */
export function readPassId(text) {
  const value = String(text ?? "").trim();
  const whole = passIdOf(value);
  if (whole) return whole;
  const id = value.toLowerCase().replace(/[\s-]/g, "");
  return ID_RE.test(id) ? id : null;
}

/** A button that copies `value`, saying so for two seconds. */
export function copyButton(value, label = "Copy") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "copy";
  const icon = document.createElement("span");
  icon.className = "i i-copy";
  icon.setAttribute("aria-hidden", "true");
  const text = document.createElement("span");
  text.textContent = label;
  button.append(icon, text);
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(value);
      icon.className = "i i-check";
      text.textContent = "Copied";
      setTimeout(() => {
        icon.className = "i i-copy";
        text.textContent = label;
      }, 2000);
    } catch {
      text.textContent = "Select and copy it";
    }
  });
  return button;
}
