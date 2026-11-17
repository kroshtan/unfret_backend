// The shop owners' page (/objection): sends the request to POST /v1/objections and says what happened. Nothing is
// stored in the browser.

const $ = (id) => document.getElementById(id);
const form = $("objection");
const status = $("objection-status");

const MESSAGES = {
  BAD_SITE: "Give the address of your shop, page or ad, such as shop.example.",
  BAD_REPLY_TO: "That e-mail address doesn't look right. Correct it, or leave it empty.",
  QUEUE_FULL: "Our queue is full right now. Please e-mail hello@unfret.net instead.",
};

function say(text, kind) {
  status.textContent = text;
  if (kind) status.dataset.kind = kind;
  else delete status.dataset.kind;
}

async function detailOf(response) {
  const body = await response.json().catch(() => ({}));
  return MESSAGES[body.error_code] ?? (typeof body.detail === "string" ? body.detail : "");
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = new FormData(form);
  const site = String(data.get("site") ?? "").trim();
  if (site.length < 4) {
    say(MESSAGES.BAD_SITE, "error");
    $("objection-site").focus();
    return;
  }
  const replyTo = String(data.get("reply_to") ?? "").trim();
  $("objection-send").disabled = true;
  say("Sending…");
  try {
    const response = await fetch("/v1/objections", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        kind: String(data.get("kind") ?? "other"),
        site,
        message: String(data.get("message") ?? "").trim(),
        reply_to: replyTo,
      }),
    });
    if (response.status === 201) {
      form.hidden = true;
      $("objection-done-text").textContent = replyTo
        ? `We have your request and will answer at ${replyTo} within a month.`
        : "We have your request. Without an e-mail address we can't answer you, but we will handle it within a month.";
      $("objection-done").hidden = false;
      $("objection-done").scrollIntoView({ block: "nearest" });
      return;
    }
    if (response.status === 422) {
      say((await detailOf(response)) || "Please check the form and try again.", "error");
    } else if (response.status === 429) {
      say("Too many requests from here today. Please e-mail hello@unfret.net instead.", "error");
    } else {
      say((await detailOf(response)) || "That didn't work. Please e-mail hello@unfret.net instead.", "error");
    }
  } catch {
    say("Couldn't reach Unfret. Check your connection, or e-mail hello@unfret.net.", "error");
  }
  $("objection-send").disabled = false;
});
