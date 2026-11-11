"use strict";

// Apply the theme before the first paint.
// Theme changes stay in the browser and never call the CivicLens API.
(() => {
    const storageKey = "civiclens-theme";
    const root = document.documentElement;
    const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");
    let preference = null;

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
            button.textContent = theme === "dark" ? "Light mode" : "Dark mode";
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