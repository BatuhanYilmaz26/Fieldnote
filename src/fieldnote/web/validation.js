// Native constraints with application-owned messages and accessible inline feedback.
// Keep validity checks local; the API remains authoritative for policy and URLs.
export function fieldMessage(field) {
  const validity = field.validity;
  if (validity.badInput) return field.type === "number" ? "Enter a valid number." : "Enter a valid value.";
  if (validity.valueMissing || (field.required && !field.value.trim())) {
    if (field.id === "topic") return "Enter a topic for your draft.";
    if (field.id === "audience") return "Tell us who this article is for.";
    if (field.id === "target_words") return "Choose a target word count.";
    return field.tagName === "SELECT" ? "Choose an option." : "Complete this field.";
  }
  if (validity.typeMismatch) return field.type === "url"
    ? "Enter a complete URL, such as https://example.com/article." : "Enter a valid value.";
  if (validity.rangeUnderflow) return `Use at least ${Number(field.min).toLocaleString("en")} words.`;
  if (validity.rangeOverflow) return `Use ${Number(field.max).toLocaleString("en")} words or fewer.`;
  if (validity.stepMismatch) return `Choose a word count in steps of ${field.step}.`;
  if (validity.tooLong) return `Use ${field.maxLength.toLocaleString("en")} characters or fewer.`;
  if (validity.tooShort) return `Use at least ${field.minLength.toLocaleString("en")} characters.`;
  if (validity.patternMismatch || validity.customError) return "Check this value and try again.";
  return "";
}

export function createFormValidation(form, { announce = () => {}, custom = {} } = {}) {
  const errors = new Map();
  const fields = () => [...form.elements].filter(field => field.id && field.willValidate);

  function show(field, message) {
    let error = errors.get(field.id);
    if (!error && message) {
      error = document.createElement("p");
      error.id = `${field.id}-error`;
      error.className = "field-error";
      (field.closest(".field") || field.parentElement).append(error);
      errors.set(field.id, error);
    }
    const description = new Set((field.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean));
    if (message) {
      error.textContent = message;
      error.hidden = false;
      description.add(error.id);
      field.setAttribute("aria-invalid", "true");
    } else {
      if (error) { error.hidden = true; error.textContent = ""; description.delete(error.id); }
      field.removeAttribute("aria-invalid");
    }
    if (description.size) field.setAttribute("aria-describedby", [...description].join(" "));
    else field.removeAttribute("aria-describedby");
    return Boolean(message);
  }

  function message(field) {
    return fieldMessage(field) || custom[field.id]?.(field) || "";
  }

  function refresh() {
    for (const field of fields()) if (errors.has(field.id)) show(field, message(field));
  }
  form.addEventListener("input", refresh);
  form.addEventListener("change", refresh);
  form.addEventListener("reset", () => { for (const field of fields()) show(field, ""); });
  // Suppress browser-owned tooltips if another caller checks a constraint.
  form.addEventListener("invalid", event => event.preventDefault(), true);

  return {
    validate() {
      const invalid = fields().filter(field => show(field, message(field)));
      if (!invalid.length) return true;
      for (let parent = invalid[0].parentElement; parent && parent !== form; parent = parent.parentElement) {
        if (parent.tagName === "DETAILS") parent.open = true;
      }
      invalid[0].focus();
      invalid[0].scrollIntoView({block: "center", behavior: "instant"});
      announce(`${invalid.length === 1 ? "One field needs" : `${invalid.length} fields need`} your attention. ${message(invalid[0])}`);
      return false;
    },
  };
}
