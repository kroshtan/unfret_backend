// The install button for the visitor's browser ([data-get="main"], with [data-get="also"] for the other ways and
// [data-get="link"] for the link checker), on the home page and wherever a page offers Unfret.

// One main button for the visitor's browser rather than a row of stores: Chrome's store for Chrome and its
// relatives (Edge, Brave, Opera and Vivaldi install from it too), Firefox Add-ons for Firefox, and the link checker,
// which installs as an app, on a phone. The other ways stay one line below. Without JavaScript: Chrome.
export function browserChoice() {
  const agent = navigator.userAgent;
  const phone = /Android|iPhone|iPad|iPod/i.test(agent) || navigator.userAgentData?.mobile === true;
  if (phone) return "phone";
  if (/Firefox\//.test(agent)) return "firefox";
  if (/Edg\//.test(agent)) return "edge";
  if (/OPR\//.test(agent)) return "opera";
  return "chrome";
}

export function chooseButtons() {
  const choice = browserChoice();
  const chrome = document.querySelector('[data-get="main"]')?.href ?? "";
  const firefox = document.querySelector('[data-get="also"] a')?.href ?? "";
  for (const main of document.querySelectorAll('[data-get="main"]')) {
    if (choice === "phone") {
      main.href = "/check";
      main.textContent = "Use Unfret on your phone";
      main.removeAttribute("rel");
    } else if (choice === "firefox") {
      main.href = firefox;
      main.textContent = "Add to Firefox";
    } else if (choice === "edge" || choice === "opera") {
      main.textContent = choice === "edge" ? "Add to Edge" : "Add to Opera";
    }
  }
  for (const link of document.querySelectorAll('[data-get="link"]')) link.hidden = choice === "phone";
  for (const also of document.querySelectorAll('[data-get="also"]')) {
    const store = document.createElement("a");
    store.rel = "noopener";
    if (choice === "firefox") {
      store.href = chrome;
      store.textContent = "Chrome, Edge and Brave";
    } else {
      store.href = firefox;
      store.textContent = "Firefox";
    }
    const phone = document.createElement("a");
    phone.href = "/extension#phone";
    phone.textContent = "your phone";
    if (choice === "phone") {
      const computer = document.createElement("a");
      computer.href = "/extension";
      computer.textContent = "Chrome, Firefox and other browsers";
      also.replaceChildren("On a computer: add Unfret to ", computer, ".");
    } else {
      also.replaceChildren("Also for ", store, " and ", phone, ".");
    }
  }
}
