const dialogFocusSelector = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "textarea:not([disabled])",
  "select:not([disabled])",
  "summary",
  '[tabindex="0"]',
].join(",");

function dialogFocusStops(dialog) {
  return [...dialog.querySelectorAll(dialogFocusSelector)]
    .filter((control) => control.getClientRects().length > 0);
}

function trapDialogTab(event) {
  if (event.key !== "Tab") return;
  const dialog = event.currentTarget;
  const stops = dialogFocusStops(dialog);
  if (!stops.length) {
    event.preventDefault();
    const fallback = dialog.querySelector("[data-dialog-busy]:not([hidden]), [role='alert']:not([hidden]), [tabindex='-1']");
    (fallback ?? dialog).focus();
    return;
  }
  const first = stops[0];
  const last = stops.at(-1);
  const activeIndex = stops.indexOf(document.activeElement);
  if (event.shiftKey && activeIndex <= 0) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && (activeIndex === -1 || document.activeElement === last)) {
    event.preventDefault();
    first.focus();
  }
}
