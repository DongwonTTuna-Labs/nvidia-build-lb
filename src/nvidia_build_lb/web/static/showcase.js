const disclosure = document.querySelector(".navigation-disclosure");
const desktopNavigation = window.matchMedia("(min-width: 1280px)");
let navigationOwnsFocus = false;
let navigationOwnsNextHashChange = false;
const headingIds = {
  buttons: "button-heading",
  inputs: "input-heading",
  statuses: "status-heading",
  tables: "table-heading",
  dialogs: "dialog-section-heading",
  "system-states": "system-states-heading",
};

function syncShowcaseNavigation(event = desktopNavigation) {
  const focusInside = disclosure.contains(document.activeElement) || navigationOwnsFocus;
  disclosure.open = event.matches;
  if (focusInside) {
    const target = event.matches
      ? disclosure.querySelector("nav a[aria-current]")
      : disclosure.querySelector(":scope > summary");
    target?.focus();
  }
}

document.addEventListener("focusin", (event) => {
  if (disclosure.contains(event.target)) navigationOwnsFocus = true;
  else if (event.target !== document.body && event.target !== document.documentElement) navigationOwnsFocus = false;
}, {capture: true});

function selectShowcaseSection(section, moveFocus) {
  const normalized = Object.hasOwn(headingIds, section) ? section : "buttons";
  for (const candidate of disclosure.querySelectorAll("nav a")) {
    candidate.removeAttribute("aria-current");
  }
  disclosure.querySelector(`nav a[href='#${normalized}']`)?.setAttribute("aria-current", "location");
  if (moveFocus) {
    window.requestAnimationFrame(() => document.getElementById(headingIds[normalized])?.focus());
  }
}

function navigateToShowcaseSection(link) {
  const section = link.getAttribute("href").slice(1);
  navigationOwnsNextHashChange = window.location.hash !== `#${section}`;
  selectShowcaseSection(section, true);
  if (!desktopNavigation.matches) disclosure.open = false;
}

desktopNavigation.addEventListener("change", syncShowcaseNavigation);
window.addEventListener("hashchange", () => {
  if (navigationOwnsNextHashChange) {
    navigationOwnsNextHashChange = false;
    return;
  }
  const section = window.location.hash.slice(1);
  if (Object.hasOwn(headingIds, section)) selectShowcaseSection(section, true);
});
for (const link of disclosure.querySelectorAll("nav a")) {
  link.addEventListener("click", () => navigateToShowcaseSection(link));
}
syncShowcaseNavigation();
const initialSection = window.location.hash.slice(1);
selectShowcaseSection(Object.hasOwn(headingIds, initialSection) ? initialSection : "buttons", false);
