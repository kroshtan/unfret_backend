// Nebulora Home, the Unfret test shop: the made-up store's scripts. Each trick works as it does on real listings.
"use strict";

(() => {
  // A countdown that starts again on every page load ("evergreen" timer): its deadline is never real.
  const settingsTag = document.getElementById("countdown-timer-settings");
  let minutes = 15;
  try {
    minutes = Number(JSON.parse(settingsTag ? settingsTag.textContent : "{}").minutes) || 15;
  } catch {
    minutes = 15;
  }
  let deadline = Date.now() + minutes * 60_000;
  const clocks = document.querySelectorAll("[data-countdown] .countdown-clock");
  const pad = (n) => String(n).padStart(2, "0");
  const tick = () => {
    let left = Math.round((deadline - Date.now()) / 1000);
    if (left <= 0) {
      deadline = Date.now() + minutes * 60_000; // and when it runs out, it quietly starts over
      left = minutes * 60;
    }
    const text = `${pad(Math.floor(left / 3600))}:${pad(Math.floor(left / 60) % 60)}:${pad(left % 60)}`;
    clocks.forEach((clock) => { clock.textContent = text; });
  };
  if (clocks.length) {
    tick();
    setInterval(tick, 1000);
  }

  // "27 people are viewing this right now": a random number.
  const viewers = document.querySelector("[data-viewers]");
  if (viewers) {
    setInterval(() => { viewers.textContent = String(18 + Math.floor(Math.random() * 17)); }, 7000);
  }

  // "Someone just bought this" pop-ups, from a fixed list.
  const pop = document.querySelector("[data-sales-pop]");
  if (pop) {
    const buyers = [
      ["Jessica in Austin, TX", "4 minutes ago"],
      ["Mike in Columbus, OH", "9 minutes ago"],
      ["Priya in San Jose, CA", "12 minutes ago"],
      ["Tom in Tampa, FL", "18 minutes ago"],
    ];
    let next = 0;
    const show = () => {
      const [who, when] = buyers[next % buyers.length];
      next += 1;
      pop.querySelector("[data-pop-buyer]").textContent = who;
      pop.querySelector("[data-pop-when]").textContent = when;
      pop.classList.add("is-shown");
      setTimeout(() => pop.classList.remove("is-shown"), 4500);
    };
    setTimeout(show, 2500);
    setInterval(show, 11000);
  }

  // The photo gallery.
  const main = document.getElementById("gallery-main");
  document.querySelectorAll(".thumb").forEach((thumb) => {
    thumb.addEventListener("click", () => {
      if (main) main.src = thumb.dataset.src;
      document.querySelectorAll(".thumb").forEach((t) => t.classList.toggle("is-active", t === thumb));
    });
  });

  // Quantity, at most "the 3 left in stock".
  const qty = document.querySelector(".qty input");
  document.querySelectorAll("[data-qty]").forEach((button) => {
    button.addEventListener("click", () => {
      if (!qty) return;
      qty.value = String(Math.min(3, Math.max(1, Number(qty.value || 1) + Number(button.dataset.qty))));
    });
  });
  document.querySelectorAll("form").forEach((form) => form.addEventListener("submit", (e) => e.preventDefault()));

  // Nothing is ever sold here.
  const pay = document.querySelector("[data-pay]");
  const note = document.querySelector("[data-pay-note]");
  if (pay && note) {
    pay.addEventListener("click", () => { note.textContent = "This is a test shop: nothing is sold here."; });
  }
})();
