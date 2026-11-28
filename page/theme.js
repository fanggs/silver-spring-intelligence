"use strict";

// Apply the theme before the first paint.
// Theme changes stay in the browser and never call the CivicLens API.
(() => {
    const storageKey = "civiclens-theme";
    const root = document.documentElement;
    const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");
    let preference = null;
    const ICON = 'width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"';
    const SUN = `<svg ${ICON}><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>`;
    const MOON = `<svg ${ICON}><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>`;

    function readPreference() {
        try {
            const saved = window.localStorage.getItem(storageKey);
            return saved === "light" || saved === "dark" ? saved : null;
        } catch {
            // Switching still works when browser storage is disabled.
            return null;
        }
    }

    function applyTheme() {
        const theme = preference || (systemTheme.matches ? "dark" : "light");
        root.dataset.theme = theme;

        document.querySelectorAll(".theme-toggle").forEach((button) => {
            button.setAttribute(
                "aria-label",
                theme === "dark" ? "Switch to light mode" : "Switch to dark mode"
            );
            // An icon, not words: a moon offers dark mode, a sun offers light
            // mode. The label above tells screen readers what it does.
            button.innerHTML = theme === "dark" ? SUN : MOON;
            button.title = theme === "dark" ? "Light mode" : "Dark mode";
        });
    }

    preference = readPreference();
    applyTheme();

    document.addEventListener("DOMContentLoaded", () => {
        document.querySelectorAll(".theme-toggle").forEach((button) => {
            button.addEventListener("click", () => {
                preference = root.dataset.theme === "dark" ? "light" : "dark";
                applyTheme();

                try {
                    window.localStorage.setItem(storageKey, preference);
                } catch {
                    // Keep the choice for this page if storage is unavailable.
                }
            });

            button.hidden = false;
        });

        applyTheme();
    });

    // Follow system changes until the visitor explicitly chooses a theme.
    if (systemTheme.addEventListener) {
        systemTheme.addEventListener("change", applyTheme);
    } else if (systemTheme.addListener) {
        systemTheme.addListener(applyTheme);
    }

    // Synchronize other open CivicLens tabs.
    window.addEventListener("storage", (event) => {
        if (event.key === storageKey || event.key === null) {
            preference = readPreference();
            applyTheme();
        }
    });
})();