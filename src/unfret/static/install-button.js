// The install button for the visitor's browser ([data-get="main"], with [data-get="also"] for the other ways and
// [data-get="link"] for the link checker), on the home page and wherever a page offers Unfret.

// One main button for the visitor's browser rather than a row of stores: Chrome's store for Chrome and its
// relatives (Edge, Brave, Opera and Vivaldi install from it too), Firefox Add-ons for Firefox. On a phone, a
// tablet or a narrow window the link checker comes first instead (a filled button, "Check a link"), and the store
// button follows as the second choice. The other ways stay one line below. Without JavaScript: Chrome first.
export function browserChoice(agent = navigator.userAgent, mobile = navigator.userAgentData?.mobile === true) {
  if (/Android|iPhone|iPad|iPod/i.test(agent) || mobile) return "phone";
  if (/Firefox\//.test(agent)) return "firefox";
  if (/Edg\//.test(agent)) return "edge";
  if (/OPR\//.test(agent)) return "opera";
  return "chrome";
}

// A narrow screen, or one used by touch (a phone, a tablet): the link checker first. The same width as the site's
// phone layout.
const SMALL_QUERIES = ["(max-width: 40rem)", "(pointer: coarse)"];

function smallScreen() {
  return SMALL_QUERIES.some((query) => window.matchMedia?.(query).matches);
}

/**
 * The buttons for a browser choice and screen: which comes first, and the store button's words.
 *
 * @param {string} choice - From browserChoice().
 * @param {boolean} small - A narrow or touch screen.
 * @returns {{linkFirst: boolean, store: "chrome" | "firefox", mainText: string, linkText: string, also: string}}
 */
export function buttonPlan(choice, small) {
  const linkFirst = small || choice === "phone";
  const store = choice === "firefox" ? "firefox" : "chrome";
  const mainText = { firefox: "Add to Firefox", edge: "Add to Edge", opera: "Add to Opera" }[choice] ?? "Add to Chrome";
  return {
    linkFirst,
    store,
    mainText,
    linkText: linkFirst ? "Check a link" : "Check a link instead",
    also: choice === "phone" ? "computer" : store === "firefox" ? "chrome" : "firefox",
  };
}

function apply(choice, stores) {
  const plan = buttonPlan(choice, smallScreen());
  for (const main of document.querySelectorAll('[data-get="main"]')) {
    main.href = stores[plan.store];
    main.textContent = plan.mainText;
    main.classList.toggle("primary", !plan.linkFirst);
    main.classList.toggle("quiet", plan.linkFirst);
    const link = main.parentElement?.querySelector('[data-get="link"]');
    if (!link) continue;
    link.textContent = plan.linkText;
    link.classList.toggle("primary", plan.linkFirst);
    link.classList.toggle("quiet", !plan.linkFirst);
    if (plan.linkFirst) main.before(link);
    else link.before(main);
  }
  for (const also of document.querySelectorAll('[data-get="also"]')) {
    if (plan.also === "computer") {
      const computer = document.createElement("a");
      computer.href = "/extension";
      computer.textContent = "Chrome, Firefox and other browsers";
      also.replaceChildren("On a computer: add Unfret to ", computer, ".");
      continue;
    }
    const store = document.createElement("a");
    store.rel = "noopener";
    store.href = stores[plan.also];
    store.textContent = plan.also === "chrome" ? "Chrome, Edge and Brave" : "Firefox";
    const phone = document.createElement("a");
    phone.href = "/extension#phone";
    phone.textContent = "your phone";
    also.replaceChildren("Also for ", store, " and ", phone, ".");
  }
}

export function chooseButtons() {
  const choice = browserChoice();
  // The store addresses, as the page has them before any change (with JavaScript off it offers Chrome's).
  const stores = {
    chrome: document.querySelector('[data-get="main"]')?.href ?? "",
    firefox: document.querySelector('[data-get="also"] a')?.href ?? "",
  };
  apply(choice, stores);
  // A window made narrower or wider (or a tablet turned) gets the other order.
  for (const query of SMALL_QUERIES) window.matchMedia?.(query).addEventListener?.("change", () => apply(choice, stores));
}
