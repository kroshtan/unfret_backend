// /developers: the Copy buttons next to the code blocks (the MCP config, the API call). The prices and the tiers'
// descriptions are put in by the server, from the same settings as /.well-known/x402.

for (const button of document.querySelectorAll("[data-copy]")) {
  button.addEventListener("click", async () => {
    const source = document.getElementById(button.dataset.copy);
    try {
      await navigator.clipboard.writeText(source.textContent.trim());
      button.textContent = "Copied";
    } catch {
      button.textContent = "Select and copy it";
    }
    setTimeout(() => {
      button.textContent = "Copy";
    }, 2000);
  });
}
