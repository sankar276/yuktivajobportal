// Runs inside the application page. Finds every form control, works out its
// human label, and reports bot checks and login walls. Read-only except for
// tagging each control with a `data-jp` attribute so it can be found again.
() => {
  const text = (node) => (node ? (node.innerText || node.textContent || "").replace(/\s+/g, " ").trim() : "");

  const visible = (el) => {
    if (!el || !el.isConnected) return false;
    const style = getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };

  // The control's label, from the most to the least explicit source.
  const labelOf = (el) => {
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const joined = labelledBy.split(/\s+/).map((id) => text(document.getElementById(id))).join(" ").trim();
      if (joined) return joined;
    }
    if (el.id) {
      const explicit = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (text(explicit)) return text(explicit);
    }
    const wrapping = el.closest("label");
    if (text(wrapping)) return text(wrapping);
    const aria = el.getAttribute("aria-label");
    if (aria && aria.trim()) return aria.trim();
    // A label-like element earlier in the same field container.
    let container = el.parentElement;
    for (let depth = 0; container && depth < 4; depth += 1, container = container.parentElement) {
      const candidate = container.querySelector("label, legend, .label, .application-label, [class*='label'], [class*='question']");
      if (candidate && !candidate.contains(el) && text(candidate)) return text(candidate);
    }
    return (el.getAttribute("placeholder") || el.getAttribute("title") || "").trim();
  };

  // The question a group of radios / checkboxes answers.
  const groupLabelOf = (el) => {
    const group = el.closest("[role='radiogroup'], [role='group'], fieldset");
    if (group) {
      const labelledBy = group.getAttribute("aria-labelledby");
      if (labelledBy && text(document.getElementById(labelledBy))) return text(document.getElementById(labelledBy));
      const legend = group.querySelector("legend");
      if (text(legend)) return text(legend);
      if (group.getAttribute("aria-label")) return group.getAttribute("aria-label").trim();
    }
    let container = el.parentElement;
    for (let depth = 0; container && depth < 5; depth += 1, container = container.parentElement) {
      const candidate = container.querySelector(".application-label, .label, [class*='question'], legend, label:not(:has(input))");
      if (candidate && !candidate.contains(el) && text(candidate)) return text(candidate);
    }
    return "";
  };

  // Required markers come in several asterisk glyphs.
  const STAR = "[*\u2731\uFF0A\u2217]";
  const isRequired = (el, label) =>
    el.required ||
    el.getAttribute("aria-required") === "true" ||
    new RegExp(`${STAR}\\s*$|^\\s*${STAR}|\\(required\\)|\\brequired\\b\\s*$`, "i").test(label);

  const cleanLabel = (label) =>
    label
      .replace(new RegExp(`\\s*${STAR}+\\s*$`), "")
      .replace(new RegExp(`^\\s*${STAR}+\\s*`), "")
      .replace(/\s*\(required\)\s*$/i, "")
      .trim();

  let counter = 0;
  const tag = (el) => {
    if (!el.dataset.jp) {
      counter += 1;
      el.dataset.jp = String(counter);
    }
    return `[data-jp="${el.dataset.jp}"]`;
  };

  const fields = [];
  const groups = new Map();
  const controls = document.querySelectorAll("input, textarea, select, [role='combobox']");

  for (const el of controls) {
    const tagName = el.tagName.toLowerCase();
    const type = tagName === "input" ? (el.getAttribute("type") || "text").toLowerCase() : tagName;
    if (["hidden", "submit", "button", "image", "reset", "search", "password"].includes(type)) continue;
    if (el.disabled || (el.readOnly && type !== "file")) continue;
    if (type !== "file" && !visible(el) && !(type === "radio" || type === "checkbox")) continue;
    // Only what a person would see and fill; widgets' internal fields are skipped.
    if (el.closest("[aria-hidden='true'], .g-recaptcha, .h-captcha")) continue;

    if (type === "radio" || type === "checkbox") {
      const optionLabel = cleanLabel(labelOf(el));
      const groupLabel = groupLabelOf(el);
      const standalone = type === "checkbox" && (!el.name || document.querySelectorAll(`input[type="checkbox"][name="${CSS.escape(el.name)}"]`).length === 1);
      if (standalone) {
        const label = optionLabel || groupLabel;
        fields.push({
          ref: tag(el), label: cleanLabel(label), type: "checkbox", name: el.name || "",
          required: isRequired(el, label), options: [{ label: "Yes", ref: tag(el) }], prefilled: el.checked,
        });
        continue;
      }
      const key = `${type}:${el.name || groupLabel}`;
      if (!groups.has(key)) {
        const entry = {
          ref: tag(el), label: cleanLabel(groupLabel || el.name || ""), type, name: el.name || "",
          required: isRequired(el, groupLabel), options: [], prefilled: false,
        };
        groups.set(key, entry);
        fields.push(entry);
      }
      const entry = groups.get(key);
      entry.options.push({ label: optionLabel || el.value, ref: tag(el) });
      entry.required = entry.required || el.required;
      entry.prefilled = entry.prefilled || el.checked;
      continue;
    }

    const rawLabel = labelOf(el);
    const field = {
      ref: tag(el), label: cleanLabel(rawLabel), name: el.getAttribute("name") || el.id || "",
      required: isRequired(el, rawLabel), options: [], prefilled: false,
    };
    if (tagName === "select") {
      field.type = "select";
      field.options = Array.from(el.options)
        .filter((option) => option.value !== "" && !option.disabled)
        .map((option) => ({ label: text(option), ref: option.value }));
      field.prefilled = el.value !== "" && el.selectedIndex > 0;
    } else if (el.getAttribute("role") === "combobox" && tagName !== "select") {
      field.type = "combobox";
      field.prefilled = !!(el.value || "").trim();
    } else if (type === "file") {
      field.type = "file";
    } else {
      field.type = tagName === "textarea" ? "textarea" : (["email", "tel", "url", "number", "date"].includes(type) ? type : "text");
      field.prefilled = !!(el.value || "").trim();
    }
    fields.push(field);
  }

  // ---- checks -------------------------------------------------------------
  const sources = Array.from(document.querySelectorAll("iframe[src], script[src]")).map((n) => n.src);
  const captchaVendors = [
    ["reCAPTCHA", /recaptcha/i], ["hCaptcha", /hcaptcha/i], ["Turnstile", /challenges\.cloudflare\.com|turnstile/i],
    ["Arkose", /arkoselabs|funcaptcha/i], ["GeeTest", /geetest/i], ["DataDome", /datadome|captcha-delivery/i],
    ["PerimeterX", /perimeterx|px-captcha/i],
  ];
  let captcha = null;
  for (const [vendor, pattern] of captchaVendors) {
    if (sources.some((src) => pattern.test(src))) { captcha = vendor; break; }
  }
  if (!captcha && document.querySelector(".g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey], #px-captcha, [class*='captcha' i], [id*='captcha' i]")) {
    captcha = "captcha";
  }
  if (!captcha && (window.grecaptcha || window.hcaptcha || window.turnstile)) captcha = "captcha";

  const hasPassword = Array.from(document.querySelectorAll("input[type='password']")).some(visible);
  const bodyText = text(document.body).slice(0, 4000);
  const login = hasPassword || /\b(sign in|log in|create an account)\b[^.]{0,40}\b(to apply|to continue|before applying)\b/i.test(bodyText);
  const interstitial = /just a moment|attention required|verify you are (a )?human|checking your browser/i.test(document.title + " " + bodyText.slice(0, 400));

  const submitCandidates = Array.from(document.querySelectorAll("button, input[type='submit']")).filter(visible);
  // Only a button that plainly says it submits the application. "Next" and
  // "Continue" belong to multi-step forms, which are handed to the person.
  const buttonText = (el) => text(el) || el.value || el.getAttribute("aria-label") || "";
  const submit = submitCandidates.find(
    (el) => /\b(submit|apply|send)\b/i.test(buttonText(el)) && !/sign in|log in|linkedin|upload|attach|next|continue|save/i.test(buttonText(el))
  );

  return {
    url: location.href,
    title: document.title,
    fields,
    captcha,
    login,
    interstitial,
    submit: submit ? tag(submit) : null,
    submitText: submit ? (text(submit) || submit.value || "") : "",
  };
}
