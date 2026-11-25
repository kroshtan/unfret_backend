// The Dutch landing page (/nl): the paste-a-link field (start-check.js). Everything else on the page is plain HTML.
import { wireStartForm } from "./start-check.js";

const form = document.getElementById("start-check");
wireStartForm(form, { empty: form?.dataset.empty ?? "", invalid: form?.dataset.invalid ?? "" });
