// The Copy buttons next to code blocks on the pages for developers (/developers, /developers/http): each
// `data-copy` button copies the text of the element it names.

export async function copy(button, text) {
  try {
    await navigator.clipboard.writeText(text);
    button.textContent = "Copied";
  } catch {
    button.textContent = "Select and copy it";
  }
  setTimeout(() => {
    button.textContent = "Copy";
  }, 2000);
}

for (const button of document.querySelectorAll("[data-copy]")) {
  button.addEventListener("click", () => copy(button, document.getElementById(button.dataset.copy).textContent.trim()));
}
