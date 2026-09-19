"use strict";

/* Change only this address when Alan gives you the deployed URL. No API keys go here. */
const API_BASE_URL = "https://silver-spring-intelligence.onrender.com";
const SCOPE_MESSAGE = "I can't answer that from the data I have. I can tell you about population, languages spoken at home, household income, foreign-born residents, housing and commuting for Silver Spring, Maryland — plus its local businesses and the Fenton Village district — and compare Silver Spring with the rest of Montgomery County.";

function initLandingPage() {
    const heroVisual = document.getElementById("heroVisual");
    const locationCard = document.getElementById("locationCard");

    if (heroVisual && locationCard && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
        heroVisual.addEventListener("mousemove", (event) => {
            const box = heroVisual.getBoundingClientRect();
            const x = (event.clientX - box.left) / box.width;
            const y = (event.clientY - box.top) / box.height;
            locationCard.style.transform = `translate(${x * 8}px, ${y * 8}px)`;
        });

        heroVisual.addEventListener("mouseleave", () => {
            locationCard.style.transform = "translate(0, 0)";
        });
    }

    document.querySelectorAll(".nav-links a[href^='#']").forEach((link) => {
        link.addEventListener("click", () => {
            document.querySelectorAll(".nav-links a").forEach((item) => item.classList.remove("active"));
            link.classList.add("active");
        });
    });
}

function initAppPage() {
    const element = (id) => document.getElementById(id);

    const ui = {
        banner: element("modeBanner"),
        form: element("askForm"),
        question: element("questionInput"),
        askButton: element("askButton"),
        state: element("answerState"),
        result: element("answerResult"),
        answer: element("answerText"),
        sources: element("sourceList"),
        rows: element("supportRows"),
        layerStatus: element("layerStatus"),
        mapFallback: element("mapFallback"),
        place: element("placeInput"),
        placeGo: element("placeGo")
    };

    let map = null;
    let tractLayer = null;
    let businessLayer = null;
    const tractLayersByGeoid = new Map();
    let activeGeoids = new Set();
    let boundaryLayer = null;
    let highlightScope = "selection";
    let demoMode = false;

    // These objects use Alan's final API field names. All demo content is synthetic.
    const demoAnswer = {
        answer: "DEMO ONLY: This is a preview of where a real, source-backed answer from Alan's server will appear.",
        highlight_geoids: ["DEMO-001", "DEMO-002"],
        table: [{ label: "Sample value — not a real statistic", value: 0 }],
        sources: [{
            label: "Example link: Census API documentation (not evidence for demo data)",
            url: "https://www.census.gov/data/developers.html"
        }]
    };

    const demoTracts = {
        type: "FeatureCollection",
        features: [
            {
                type: "Feature",
                properties: { geoid: "DEMO-001", name: "Synthetic demo tract 1" },
                geometry: {
                    type: "Polygon",
                    coordinates: [[
                        [-77.046, 38.977], [-77.026, 38.977], [-77.026, 38.995],
                        [-77.046, 38.995], [-77.046, 38.977]
                    ]]
                }
            },
            {
                type: "Feature",
                properties: { geoid: "DEMO-002", name: "Synthetic demo tract 2" },
                geometry: {
                    type: "Polygon",
                    coordinates: [[
                        [-77.026, 38.977], [-77.007, 38.977], [-77.007, 38.995],
                        [-77.026, 38.995], [-77.026, 38.977]
                    ]]
                }
            },
            {
                type: "Feature",
                properties: { geoid: "DEMO-003", name: "Synthetic demo tract 3" },
                geometry: {
                    type: "Polygon",
                    coordinates: [[
                        [-77.046, 38.995], [-77.026, 38.995], [-77.026, 39.012],
                        [-77.046, 39.012], [-77.046, 38.995]
                    ]]
                }
            },
            {
                type: "Feature",
                properties: { geoid: "DEMO-004", name: "Synthetic demo tract 4" },
                geometry: {
                    type: "Polygon",
                    coordinates: [[
                        [-77.026, 38.995], [-77.007, 38.995], [-77.007, 39.012],
                        [-77.026, 39.012], [-77.026, 38.995]
                    ]]
                }
            }
        ]
    };

    const demoBusinesses = [
        {
            name: "Synthetic demo business A",
            category: "example",
            latitude: 38.99487,
            longitude: -77.02489,
            geoid: "DEMO-002"
        },
        {
            name: "Synthetic demo business B",
            category: "example",
            latitude: 38.988,
            longitude: -77.031,
            geoid: "DEMO-001"
        }
    ];

    function setBanner(message, kind = "") {
        ui.banner.textContent = message;
        ui.banner.className = `app-banner${kind ? ` is-${kind}` : ""}`;
    }

    function showMessage(message, isError = false) {
        ui.result.hidden = true;
        ui.state.hidden = false;
        ui.state.textContent = message;
        ui.state.classList.toggle("is-error", isError);
    }

    function validUrl(value) {
        if (typeof value !== "string" || !/^https?:\/\//i.test(value.trim())) return null;
        try {
            return new URL(value).href;
        } catch (_error) {
            return null;
        }
    }

    function renderAnswer(payload) {
        // These four names are the exact /ask response contract.
        const answer = typeof payload?.answer === "string" ? payload.answer.trim() : "";
        const highlights = Array.isArray(payload?.highlight_geoids) ? payload.highlight_geoids : [];
        const table = Array.isArray(payload?.table) ? payload.table : [];
        const sources = Array.isArray(payload?.sources) ? payload.sources : [];

        activeGeoids = new Set(highlights.map(String));
        // "area" means the answer is about Silver Spring as a whole, not a
        // selection inside it, so the map shades it softly instead of
        // lighting 19 tracts up as if they were a result.
        highlightScope = payload?.highlight_scope === "area" ? "area" : "selection";
        updateHighlights();

        if (!answer) {
            showMessage(SCOPE_MESSAGE, true);
            return;
        }

        ui.state.hidden = true;
        ui.result.hidden = false;
        ui.answer.textContent = answer;
        ui.sources.replaceChildren();

        sources.forEach((source) => {
            const href = validUrl(source?.url);
            if (!href) return;

            const item = document.createElement("li");
            const link = document.createElement("a");
            link.href = href;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            link.textContent = String(source.label || href);
            item.appendChild(link);
            ui.sources.appendChild(item);
        });

        if (!ui.sources.children.length) {
            const item = document.createElement("li");
            item.textContent = "No clickable sources were returned. This answer is not verified.";
            ui.sources.appendChild(item);
        }

        // Name the columns after what this particular answer returned.
        const heads = payload.table_headers || {};
        const labelHead = document.getElementById("supportLabelHead");
        const valueHead = document.getElementById("supportValueHead");
        if (labelHead) labelHead.textContent = heads.label || "Result";
        if (valueHead) valueHead.textContent = heads.value || "Value";

        ui.rows.replaceChildren();

        table.forEach((row) => {
            if (!row || typeof row !== "object") return;

            const tr = document.createElement("tr");
            // Both cells are <td> so the columns line up — a <th> row header
            // picks up different default padding and weight.
            const label = document.createElement("td");
            label.className = "support-label";
            label.textContent = String(row.label ?? "—");

            const value = document.createElement("td");
            value.textContent =
                row.value === null || row.value === undefined
                    ? "Not available"
                    : String(row.value);

            tr.append(label, value);
            ui.rows.appendChild(tr);
        });

        if (!ui.rows.children.length) {
            const tr = document.createElement("tr");
            const td = document.createElement("td");
            td.colSpan = 2;
            td.textContent = "No supporting rows were returned.";
            tr.appendChild(td);
            ui.rows.appendChild(tr);
        }
    }

    function baseTractStyle() {
        return {
            color: "#40634b",
            weight: 1,
            fillColor: "#9fbea6",
            fillOpacity: 0.32
        };
    }

    function updateHighlights() {
        if (!map) return;

        const selectedLayers = [];

        tractLayersByGeoid.forEach((layers, geoid) => {
            const selected = activeGeoids.has(geoid);

            layers.forEach((layer) => {
                layer.setStyle(
                    selected
                        ? (highlightScope === "area"
                            ? {
                                color: "#c98a5e",
                                weight: 1,
                                fillColor: "#f3b98f",
                                fillOpacity: 0.38
                            }
                            : {
                                color: "#b9422a",
                                weight: 3,
                                fillColor: "#ff795f",
                                fillOpacity: 0.68
                            })
                        : baseTractStyle()
                );

                if (selected) {
                    if (highlightScope !== "area") layer.bringToFront();
                    selectedLayers.push(layer);
                }
            });
        });

        if (selectedLayers.length && highlightScope !== "area") {
            const bounds = L.featureGroup(selectedLayers).getBounds();
            if (bounds.isValid()) {
                map.fitBounds(bounds.pad(0.25), { maxZoom: 13 });
            }
        } else if (highlightScope === "area" && boundaryLayer) {
            const bounds = boundaryLayer.getBounds();
            if (bounds.isValid()) map.fitBounds(bounds.pad(0.08));
        }
    }

    function drawBoundary(geojson) {
        if (!map || geojson?.type !== "FeatureCollection") return;

        if (boundaryLayer) map.removeLayer(boundaryLayer);

        boundaryLayer = L.geoJSON(geojson, {
            interactive: false,
            style: {
                color: "#1d3326",
                weight: 3,
                opacity: 0.9,
                dashArray: "6 4",
                fill: false
            }
        }).addTo(map);

        const bounds = boundaryLayer.getBounds();
        if (bounds.isValid()) map.fitBounds(bounds.pad(0.08));
    }


    function drawTracts(geojson) {
        if (!map) return;

        if (geojson?.type !== "FeatureCollection" || !Array.isArray(geojson.features)) {
            throw new Error("/tracts did not return a GeoJSON FeatureCollection.");
        }

        if (tractLayer) map.removeLayer(tractLayer);
        tractLayersByGeoid.clear();

        tractLayer = L.geoJSON(geojson, {
            style: baseTractStyle,

            onEachFeature(feature, layer) {
                const geoid = feature.properties?.geoid;
                if (geoid === undefined || geoid === null) return;

                const id = String(geoid); // Preserve leading zeroes.
                const layers = tractLayersByGeoid.get(id) || [];
                layers.push(layer);
                tractLayersByGeoid.set(id, layers);

                const popup = document.createElement("div");
                const title = document.createElement("strong");
                title.textContent = String(
                    feature.properties?.name ||
                    feature.properties?.tract_name ||
                    "Census tract"
                );

                const code = document.createElement("div");
                code.textContent = `GEOID: ${id}`;

                popup.append(title, code);
                layer.bindPopup(popup);
            }
        }).addTo(map);

        updateHighlights();
    }

    function drawBusinesses(payload) {
    if (!map) return;

    const points = Array.isArray(payload) ? payload : payload?.businesses;

    if (!Array.isArray(points)) {
        throw new Error("/businesses did not return a point array.");
    }

        if (businessLayer) map.removeLayer(businessLayer);
        businessLayer = L.layerGroup().addTo(map);
        const canvas = L.canvas();

        points.forEach((point) => {
            const latitude = Number(point?.latitude);
            const longitude = Number(point?.longitude);

            if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return;

            const pin = L.circleMarker([latitude, longitude], {
                renderer: canvas,
                radius: 5,
                color: "#ffffff",
                weight: 1.5,
                fillColor: "#20382b",
                fillOpacity: 0.95
            });

            const popup = document.createElement("div");
            const title = document.createElement("strong");
            title.textContent = String(point.name || "Business");

            const category = document.createElement("div");
            category.textContent = String(point.category || "Category unavailable");

            popup.append(title, category);
            pin.bindPopup(popup);
            pin.addTo(businessLayer);
        });
    }

    function initMap() {
        if (typeof L === "undefined") {
            ui.mapFallback.hidden = false;
            ui.layerStatus.textContent = "Map library unavailable; answers can still be requested.";
            return;
        }

        // Open on wider Silver Spring, not Fenton Village alone.
        map = L.map("map").setView([38.9907, -77.0261], 12);

        L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
            maxZoom: 19,
            attribution:
                '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>'
        }).addTo(map);
    }

    async function requestJson(path, options = {}) {
        const controller = new AbortController();
        const timer = window.setTimeout(() => controller.abort(), 20000);

        try {
            const response = await fetch(`${API_BASE_URL}${path}`, {
                ...options,
                headers: {
                    Accept: "application/json",
                    ...(options.headers || {})
                },
                signal: controller.signal
            });

            if (!response.ok) {
                throw new Error(`${path} returned HTTP ${response.status}`);
            }

            return await response.json();
        } finally {
            window.clearTimeout(timer);
        }
    }

    function enableDemo(reason) {
        demoMode = true;
        setBanner(
            `DEMO MODE — synthetic polygons, pins, answer, and value. No Census results are shown. ${reason}`,
            "demo"
        );
        drawTracts(demoTracts);
        drawBusinesses(demoBusinesses);
        renderAnswer(demoAnswer);
        ui.layerStatus.textContent =
            "Showing synthetic example layers while the API is unavailable.";
    }

    async function loadLayers() {
        const [tracts, businesses, boundary] = await Promise.allSettled([
            requestJson("/tracts"),
            requestJson("/businesses"),
            requestJson("/boundary")
        ]);

        const bothNetworkFailures =
            tracts.status === "rejected" &&
            businesses.status === "rejected" &&
            tracts.reason instanceof TypeError &&
            businesses.reason instanceof TypeError;

        if (bothNetworkFailures) {
            enableDemo(
                "The backend could not be reached (or CORS blocked it). Refresh when it is running."
            );
            return;
        }

        const failures = [];

        if (tracts.status === "fulfilled") {
            try {
                drawTracts(tracts.value);
            } catch (error) {
                failures.push(error.message);
            }
        } else {
            failures.push(tracts.reason.message);
        }

        if (businesses.status === "fulfilled") {
            try {
                drawBusinesses(businesses.value);
            } catch (error) {
                failures.push(error.message);
            }
        } else {
            failures.push(businesses.reason.message);
        }

        // Drawn last so the Silver Spring outline sits above the tract fill,
        // and so it wins the final fitBounds.
        if (boundary.status === "fulfilled") {
            try {
                drawBoundary(boundary.value);
            } catch (error) {
                failures.push(error.message);
            }
        }

        setBanner(
            failures.length
                ? `Live server reached, but some map data is unavailable: ${failures.join("; ")}`
                : "LIVE DATA — connected to Alan's server.",
            failures.length ? "warning" : "live"
        );

        ui.layerStatus.textContent = failures.length
            ? "Some map layers could not load; you can still ask a question."
            : "County tract polygons and business pins loaded.";
    }

    ui.form.addEventListener("submit", async (event) => {
        event.preventDefault();

        const question = ui.question.value.trim();
        if (!question) return;

        ui.askButton.disabled = true;
        showMessage("Checking the available data…");
        activeGeoids = new Set();
        updateHighlights();

        try {
            if (demoMode) {
                renderAnswer(demoAnswer);
            } else {
                const payload = await requestJson("/ask", {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json",
                        Accept: "application/json"
                    },
                    body: JSON.stringify({ question }) // Exact request: no extra fields.
                });

                renderAnswer(payload);
            }
        } catch (error) {
            console.warn("CivicLens answer request failed:", error);
            showMessage(
                `The server could not answer this question right now. ${SCOPE_MESSAGE}`,
                true
            );
        } finally {
            ui.askButton.disabled = false;
        }
    });

    const placeViews = {
        "silver spring": [[38.9907, -77.0261], 12],
        "fenton village": [[38.99487, -77.02489], 15],
        bethesda: [[38.9847, -77.0947], 13],
        rockville: [[39.0839, -77.1528], 13]
    };

    ui.placeGo.addEventListener("click", () => {
        const search = ui.place.value.trim().toLowerCase();

        if (!map) {
            ui.layerStatus.textContent = "The map is unavailable right now.";
            return;
        }

        if (placeViews[search]) {
            map.setView(...placeViews[search]);
            ui.layerStatus.textContent = `Map centered on ${ui.place.value.trim()}.`;
            return;
        }

        const tract = tractLayersByGeoid.get(ui.place.value.trim());

        if (tract?.length) {
            map.fitBounds(L.featureGroup(tract).getBounds(), { maxZoom: 15 });
            ui.layerStatus.textContent =
                `Map centered on tract ${ui.place.value.trim()}.`;
            return;
        }

        ui.layerStatus.textContent =
            "Place not found. Try Silver Spring, Fenton Village, Bethesda, Rockville, or a tract GEOID.";
    });

    ui.place.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
            event.preventDefault();
            ui.placeGo.click();
        }
    });

    initMap();

    loadLayers().catch((error) => {
        console.warn("CivicLens map data failed:", error);
        setBanner(
            "Could not load map data. Check the server address and try refreshing.",
            "warning"
        );
        ui.layerStatus.textContent =
            "Map layers unavailable; questions may still work.";
    });
}

if (document.getElementById("askForm")) {
    initAppPage();
} else {
    initLandingPage();
}