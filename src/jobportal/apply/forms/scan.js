// Runs inside the application page. Finds the application form, every control
// in it that a person could see and fill, the label that really belongs to
// each one, and reports bot checks and login walls. Read-only except for
// tagging each control with a `data-jp` attribute so it can be found again.
//
// The rules lean one way: a control whose question cannot be established is
// reported without a label (and is then left alone or handed to the person),
// never with a neighbour's label.
() => {
  const text = (node) => (node ? (node.innerText || node.textContent || "").replace(/\s+/g, " ").trim() : "");
  const CONTROLS = "input, textarea, select, [role='combobox']";
  const SKIP_TYPES = ["hidden", "submit", "button", "image", "reset", "search", "password"];

  // What a person can actually see. Honeypot fields are typically parked
  // off-screen, made transparent or clipped to nothing rather than hidden
  // with display:none, so all of those count as not visible.
  const visible = (el) => {
    if (!el || !el.isConnected) return false;
    if (el.checkVisibility && !el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return false;
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (style.display === "none" || style.visibility === "hidden" || parseFloat(style.opacity) === 0) return false;
    }
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return false;
    if (/rect\(\s*0(px)?[\s,]+0(px)?[\s,]+0(px)?[\s,]+0(px)?\s*\)|rect\(\s*1px[\s,]+1px[\s,]+1px[\s,]+1px\s*\)/.test(style.clip || "")) return false;
    if (/inset\(\s*(50%|100%)/.test(style.clipPath || "")) return false;
    const pageWidth = Math.max(document.documentElement.scrollWidth, window.innerWidth);
    const pageHeight = Math.max(document.documentElement.scrollHeight, window.innerHeight);
    const left = rect.left + window.scrollX;
    const top = rect.top + window.scrollY;
    return left + rect.width > 0 && top + rect.height > 0 && left < pageWidth && top < pageHeight;
  };

  const typeOf = (el) => {
    const tagName = el.tagName.toLowerCase();
    return tagName === "input" ? (el.getAttribute("type") || "text").toLowerCase() : tagName;
  };
  const isChoice = (el) => ["radio", "checkbox"].includes(typeOf(el));

  // A label explicitly bound to the control: [text, how it is bound].
  const boundLabel = (el) => {
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const joined = labelledBy.split(/\s+/).map((id) => text(document.getElementById(id))).join(" ").trim();
      if (joined) return [joined, "aria-labelledby"];
    }
    if (el.id) {
      const explicit = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (text(explicit)) return [text(explicit), "for"];
    }
    const wrapping = el.closest("label");
    if (wrapping && text(wrapping)) return [text(wrapping), "wrap"];
    const aria = el.getAttribute("aria-label");
    if (aria && aria.trim()) return [aria.trim(), "aria-label"];
    return ["", ""];
  };

  // Is a control's label on show? Custom-styled radios and checkboxes hide
  // the native input and show its label instead.
  const labelVisible = (el) => {
    const explicit = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
    return visible(explicit) || visible(el.closest("label"));
  };

  // The largest ancestor that holds no control other than `members`: the
  // field's own box. Anything label-like inside it can only be about this
  // field, which is what makes the unbound fallback below safe.
  const ownContainer = (members, stop) => {
    const set = new Set(members);
    let best = null;
    for (let node = members[0].parentElement; node && node !== stop.parentElement; node = node.parentElement) {
      const others = Array.from(node.querySelectorAll(CONTROLS)).filter(
        (other) => !set.has(other) && !SKIP_TYPES.includes(typeOf(other))
      );
      if (others.length) break;
      best = node;
      if (node === stop) break;
    }
    return best;
  };

  // A label-like element inside the field's own box that is not bound to anything.
  const nearbyLabel = (members, stop) => {
    const box = ownContainer(members, stop);
    if (!box) return "";
    const candidates = box.querySelectorAll("legend, label, .label, .application-label, [class*='label'], [class*='question']");
    for (const candidate of candidates) {
      if (candidate.matches("label") && (candidate.htmlFor || candidate.querySelector(CONTROLS))) continue;
      if (members.some((member) => candidate.contains(member))) continue;
      if (text(candidate)) return text(candidate);
    }
    // No recognisable label element: whatever text the field's own box holds
    // besides the control and its options is the question. Not when the box
    // is the whole form, and not when it is too long to be one.
    if (box === stop) return "";
    const copy = box.cloneNode(true);
    copy.querySelectorAll(`${CONTROLS}, label, option, button, script, style`).forEach((node) => node.remove());
    const rest = text(copy);
    return rest.length <= 300 ? rest : "";
  };

  // Required markers come in several asterisk glyphs.
  const STAR = "[*✱＊∗]";
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

  const usable = (el) => {
    const type = typeOf(el);
    if (SKIP_TYPES.includes(type)) return false;
    if (el.disabled || (el.readOnly && type !== "file")) return false;
    // Only what a person would see and fill; widgets' internal fields are skipped.
    if (el.closest("[aria-hidden='true'], .g-recaptcha, .h-captcha")) return false;
    if (type === "file") return true; // file inputs are routinely hidden behind a button
    if (isChoice(el)) return visible(el) || labelVisible(el);
    return visible(el);
  };

  // ---- the application form ------------------------------------------------
  // Pages carry other forms too (search, job alerts, newsletter). Only the
  // form that takes the application is read: the one with a file upload, or
  // failing that the one with the most fields. Pages built without <form>
  // elements are read as a whole.
  const forms = Array.from(document.forms).map((form) => ({
    form,
    controls: Array.from(form.querySelectorAll(CONTROLS)).filter(usable),
  })).filter((entry) => entry.controls.length);
  forms.sort((a, b) => {
    const files = (entry) => (entry.controls.some((el) => typeOf(el) === "file") ? 1 : 0);
    return files(b) - files(a) || b.controls.length - a.controls.length;
  });
  const root = forms.length ? forms[0].form : document.body;
  const controls = root ? Array.from(root.querySelectorAll(CONTROLS)).filter(usable) : [];

  const fields = [];
  const groups = new Map();

  for (const el of controls) {
    const tagName = el.tagName.toLowerCase();
    const type = typeOf(el);

    if (isChoice(el)) {
      const sameName = el.name
        ? Array.from(root.querySelectorAll(`input[type="${type}"][name="${CSS.escape(el.name)}"]`)).filter(usable)
        : [el];
      const [optionText] = boundLabel(el);
      const optionLabel = cleanLabel(optionText);
      const standalone = type === "checkbox" && sameName.length === 1;
      if (standalone) {
        const [bound, source] = boundLabel(el);
        const label = bound || nearbyLabel([el], root);
        fields.push({
          ref: tag(el), label: cleanLabel(label), labelSource: bound ? source : (label ? "nearby" : ""),
          type: "checkbox", name: el.name || "", required: isRequired(el, label),
          options: [{ label: "Yes", ref: tag(el) }], prefilled: el.checked, current: el.checked ? "Yes" : "",
        });
        continue;
      }
      const key = `${type}:${el.name || tag(el)}`;
      if (!groups.has(key)) {
        // The question a group answers: its fieldset or ARIA group first.
        let label = "";
        let source = "";
        const group = el.closest("[role='radiogroup'], [role='group'], fieldset");
        if (group && sameName.every((member) => group.contains(member))) {
          const labelledBy = group.getAttribute("aria-labelledby");
          if (labelledBy && text(document.getElementById(labelledBy))) { label = text(document.getElementById(labelledBy)); source = "group"; }
          else if (text(group.querySelector(":scope > legend"))) { label = text(group.querySelector(":scope > legend")); source = "legend"; }
          else if (group.getAttribute("aria-label")) { label = group.getAttribute("aria-label").trim(); source = "group"; }
        }
        if (!label) {
          label = nearbyLabel(sameName, root);
          source = label ? "nearby" : "";
        }
        const entry = {
          ref: tag(el), label: cleanLabel(label), labelSource: source, type, name: el.name || "",
          required: isRequired(el, label), options: [], prefilled: false, current: "",
        };
        groups.set(key, entry);
        fields.push(entry);
      }
      const entry = groups.get(key);
      const shown = optionLabel || el.value;
      entry.options.push({ label: shown, ref: tag(el) });
      entry.required = entry.required || el.required;
      if (el.checked) {
        entry.prefilled = true;
        entry.current = entry.current ? `${entry.current}; ${shown}` : shown;
      }
      continue;
    }

    const [bound, source] = boundLabel(el);
    let rawLabel = bound;
    let labelSource = source;
    if (!rawLabel) {
      rawLabel = nearbyLabel([el], root);
      labelSource = rawLabel ? "nearby" : "";
    }
    if (!rawLabel) {
      rawLabel = (el.getAttribute("placeholder") || el.getAttribute("title") || "").trim();
      labelSource = rawLabel ? "placeholder" : "";
    }
    const field = {
      ref: tag(el), label: cleanLabel(rawLabel), labelSource, name: el.getAttribute("name") || el.id || "",
      required: isRequired(el, rawLabel), options: [], prefilled: false, current: "",
    };
    if (tagName === "select") {
      field.type = "select";
      field.options = Array.from(el.options)
        .filter((option) => option.value !== "" && !option.disabled)
        .map((option) => ({ label: text(option), ref: option.value }));
      field.prefilled = el.value !== "" && el.selectedIndex >= 0 && !!el.options[el.selectedIndex] && el.options[el.selectedIndex].value !== "";
      field.current = field.prefilled ? text(el.options[el.selectedIndex]) : "";
    } else if (el.getAttribute("role") === "combobox" && tagName !== "select") {
      field.type = "combobox";
      field.current = (el.value || "").trim().slice(0, 200);
      field.prefilled = !!field.current;
    } else if (type === "file") {
      field.type = "file";
    } else {
      field.type = tagName === "textarea" ? "textarea" : (["email", "tel", "url", "number", "date"].includes(type) ? type : "text");
      field.current = (el.value || "").trim().slice(0, 200);
      field.prefilled = !!field.current;
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

  // ---- the submit button ----------------------------------------------------
  // Only a button of the application form itself that plainly says it sends
  // the application. "Next" and "Continue" belong to multi-step forms, which
  // are handed to the person; so is a form with more than one such button.
  const buttonText = (el) => text(el) || el.value || el.getAttribute("aria-label") || "";
  const says = (el) =>
    /\b(submit|apply|send)\b/i.test(buttonText(el)) &&
    !/sign in|log in|linkedin|upload|attach|next|continue|save|alert|subscribe|newsletter|notify|search|share|refer|code|later/i.test(buttonText(el));
  const scope = root || document;
  let candidates = Array.from(scope.querySelectorAll("button, input[type='submit']")).filter(visible).filter(says);
  const real = candidates.filter((el) => (el.getAttribute("type") || "submit").toLowerCase() === "submit");
  if (real.length) candidates = real;
  const submit = candidates.length === 1 ? candidates[0] : null;

  return {
    url: location.href,
    title: document.title,
    fields,
    captcha,
    login,
    interstitial,
    submit: submit ? tag(submit) : null,
    submitText: submit ? buttonText(submit) : "",
    submitCandidates: candidates.length,
  };
}
