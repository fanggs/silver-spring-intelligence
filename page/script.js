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
        mapFallback: element("mapFallback")
    };

    let map = null;
    let tractLayer = null;
    let businessLayer = null;
    const tractLayersByGeoid = new Map();
    let activeGeoids = new Set();
    let boundaryLayer = null;
    let legendControl = null;
    let businessPoints = [];
    const placeNamesByGeoid = new Map();
    const tractPropsByGeoid = new Map();
    const tractGeometryByGeoid = new Map();
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
        refreshLegendForSelection();

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

    // How close a click has to land on a dot to count as clicking it. A
    // 6px dot is a small target, so we allow a little slack.
    const DOT_HIT_RADIUS = 11;

    function pointInRing(x, y, ring) {
        let inside = false;
        for (let i = 0, n = ring.length, j = n - 1; i < n; j = i++) {
            const [x1, y1] = ring[i];
            const [x2, y2] = ring[j];
            if ((y1 > y) !== (y2 > y) && x < ((x2 - x1) * (y - y1)) / (y2 - y1) + x1) {
                inside = !inside;
            }
        }
        return inside;
    }

    function pointInGeometry(lng, lat, geom) {
        if (!geom) return false;
        const polys = geom.type === "MultiPolygon" ? geom.coordinates : [geom.coordinates];
        return polys.some((poly) =>
            pointInRing(lng, lat, poly[0]) &&
            !poly.slice(1).some((hole) => pointInRing(lng, lat, hole))
        );
    }

    function nearestBusiness(latlng) {
        if (!businessPoints.length) return null;
        const origin = map.latLngToContainerPoint(latlng);
        let best = null;
        let bestDistance = DOT_HIT_RADIUS;
        businessPoints.forEach((b) => {
            const p = map.latLngToContainerPoint([b.lat, b.lng]);
            const d = Math.hypot(p.x - origin.x, p.y - origin.y);
            if (d <= bestDistance) {
                bestDistance = d;
                best = b;
            }
        });
        return best;
    }

    function tractAt(latlng) {
        for (const [geoid, geom] of tractGeometryByGeoid) {
            if (pointInGeometry(latlng.lng, latlng.lat, geom)) return geoid;
        }
        return null;
    }

    function handleMapClick(event) {
        const business = nearestBusiness(event.latlng);
        if (business) {
            L.popup()
                .setLatLng([business.lat, business.lng])
                .setContent(businessPopupContent(business.point, business.group))
                .openOn(map);
            return;
        }

        const geoid = tractAt(event.latlng);
        if (!geoid) return;

        // Clicking an area selects it, the same as an answer highlighting it,
        // so the map is explorable without asking a question first.
        activeGeoids = new Set([geoid]);
        highlightScope = "selection";
        updateHighlights({ keepView: true });
        refreshLegendForSelection();

        L.popup()
            .setLatLng(event.latlng)
            .setContent(tractPopupContent(geoid, tractPropsByGeoid.get(geoid) || {}))
            .openOn(map);
    }


    function baseTractStyle() {
        return {
            color: "#40634b",
            weight: 1,
            fillColor: "#9fbea6",
            fillOpacity: 0.32
        };
    }

    function updateHighlights(options) {
        if (!map) return;
        const keepView = Boolean(options && options.keepView);

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

        if (keepView) return;

        if (selectedLayers.length && highlightScope !== "area") {
            const bounds = L.featureGroup(selectedLayers).getBounds();
            if (bounds.isValid()) {
                map.fitBounds(bounds.pad(0.25), { maxZoom: 13 });
            }
        } else if (highlightScope === "area") {
            fitToBoundary();
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

        fitToBoundary();
    }


    function fitToBoundary() {
        if (!map || !boundaryLayer) return;
        const bounds = boundaryLayer.getBounds();
        if (!bounds.isValid()) return;
        if (!map.getContainer().clientWidth) return;

        // Leaflet measures the container to work out the zoom. The map card
        // is laid out after the script runs, so on first paint it can measure
        // a box far smaller than the real one and zoom all the way out to
        // "fit" Silver Spring. invalidateSize re-measures before we fit.
        map.invalidateSize();
        map.fitBounds(bounds.pad(0.08));
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
            // Clicks are handled once, on the map. Letting each layer claim
            // its own means whichever happens to be on top wins, and the
            // business dots sit on top of every tract.
            interactive: false,

            onEachFeature(feature, layer) {
                const geoid = feature.properties?.geoid;
                if (geoid === undefined || geoid === null) return;

                const id = String(geoid); // Preserve leading zeroes.
                const layers = tractLayersByGeoid.get(id) || [];
                layers.push(layer);
                tractLayersByGeoid.set(id, layers);

                const props = feature.properties || {};
                const placeNames = Array.isArray(props.place_names) ? props.place_names : [];
                placeNamesByGeoid.set(id, placeNames);
                // "Census Tract 7025.01" means nothing to someone standing in
                // it. Lead with what the place is called and keep the tract as
                // the reference, which is what the figures are published for.
                const tractLabel = String(props.tract_name || "Census tract").split(";")[0].trim();

                tractPropsByGeoid.set(id, props);
                tractGeometryByGeoid.set(id, feature.geometry);


            }
        }).addTo(map);

        updateHighlights();
    }


    function tractPopupContent(id, props) {
                const placeNames = Array.isArray(props.place_names) ? props.place_names : [];
                const tractLabel = String(props.tract_name || "Census tract").split(";")[0].trim();

                const popup = document.createElement("div");

                const title = document.createElement("strong");
                title.textContent = placeNames.length
                    ? placeNames.slice(0, 3).join(" · ")
                    : tractLabel;
                popup.append(title);

                if (placeNames.length > 3) {
                    const more = document.createElement("div");
                    more.className = "popup-group";
                    more.textContent = `and ${placeNames.length - 3} more`;
                    popup.append(more);
                }

                const facts = document.createElement("div");
                facts.className = "popup-facts";
                const bits = [];
                if (Number.isFinite(Number(props.population_count))) {
                    bits.push(`${Number(props.population_count).toLocaleString()} residents`);
                }
                // 250001 is the Census ceiling, not an income. Never show it.
                const income = Number(props.median_income);
                if (Number.isFinite(income) && income > 0 && income < 250001) {
                    bits.push(`median income $${income.toLocaleString()}`);
                }
                facts.textContent = bits.join(" · ");
                if (bits.length) popup.append(facts);

                const code = document.createElement("div");
                code.className = "popup-group";
                code.textContent = placeNames.length ? tractLabel : `GEOID ${id}`;
                popup.append(code);

                return popup;
    }

    // 91 raw OpenStreetMap categories is too many to colour individually, so
    // they collapse into six buckets a person can hold in their head. Order
    // matters: the first match wins.
    const BUSINESS_GROUPS = [
        {
            key: "food",
            label: "Food & drink",
            color: "#c2452d",
            test: /restaurant|cafe|coffee|fast.?food|bar\b|pub|bakery|ice.?cream|alcohol|beverage|deli|food/i
        },
        {
            key: "shop",
            label: "Shops & groceries",
            color: "#d98a1f",
            test: /grocer|supermarket|convenience|clothes|shoe|book|pet|florist|gift|hardware|paint|furniture|jewel|tobacco|variety|shop|store|market/i
        },
        {
            key: "service",
            label: "Personal & professional services",
            color: "#2f7699",
            test: /hairdress|beauty|barber|nail|spa|bank|atm|insurance|estate|laundry|dry.?clean|repair|tattoo|dentist|doctor|pharmac|clinic|optic|veterinar|storage|copyshop|travel/i
        },
        {
            key: "civic",
            label: "Community & civic",
            color: "#6b4f9e",
            test: /worship|church|school|college|university|library|social|communit|fire.?station|police|theatre|cinema|arts|museum|townhall|public.?bookcase|kindergarten|childcare/i
        },
        {
            key: "transport",
            label: "Transport & parking",
            color: "#3f7d4f",
            test: /parking|fuel|charging|bicycle|bus|taxi|car.?rental|car.?sharing|station/i
        }
    ];
    const OTHER_GROUP = { key: "other", label: "Everything else", color: "#6f6f6f" };

    function businessGroup(category) {
        const text = String(category || "");
        return BUSINESS_GROUPS.find((g) => g.test.test(text)) || OTHER_GROUP;
    }

    function refreshLegendForSelection() {
        if (!businessPoints.length) return;

        // An answer about one district should not sit next to a legend
        // counting the whole town, so the counts follow the highlight.
        const scoped = highlightScope === "selection" && activeGeoids.size;
        const points = scoped
            ? businessPoints.filter((p) => activeGeoids.has(p.geoid))
            : businessPoints;

        const counts = {};
        const detail = {};
        points.forEach((p) => {
            counts[p.key] = (counts[p.key] || 0) + 1;
            detail[p.key] = detail[p.key] || {};
            detail[p.key][p.category] = (detail[p.key][p.category] || 0) + 1;
        });
        drawBusinessLegend(counts, points.length, scoped, detail);
    }


    function describeGroup(breakdown) {
        // "Food & drink: 38" next to an answer saying "34 restaurants" reads
        // like a contradiction until you can see the bucket's contents.
        if (!breakdown) return "";
        return Object.entries(breakdown)
            .sort((a, b) => b[1] - a[1])
            .map(([name, n]) => `${name} ${n}`)
            .join(", ");
    }


    function drawBusinessLegend(counts, total, scoped, detail) {
        if (!map) return;
        if (legendControl) map.removeControl(legendControl);

        legendControl = L.control({ position: "topright" });
        legendControl.onAdd = function () {
            const box = L.DomUtil.create("div", "map-legend");

            const title = L.DomUtil.create("h4", "", box);
            title.textContent = scoped ? "Businesses here" : "Businesses";

            [...BUSINESS_GROUPS, OTHER_GROUP].forEach((g) => {
                const n = counts[g.key] || 0;
                if (!n) return;
                const row = L.DomUtil.create("div", "map-legend-row", box);
                const inside = describeGroup(detail && detail[g.key]);
                if (inside) row.title = inside;

                const dot = L.DomUtil.create("span", "map-legend-dot", row);
                dot.style.background = g.color;

                const label = L.DomUtil.create("span", "map-legend-label", row);
                label.textContent = g.label;

                const count = L.DomUtil.create("span", "map-legend-count", row);
                count.textContent = String(n);
            });

            if (!total) {
                const empty = L.DomUtil.create("div", "map-legend-label", box);
                empty.textContent = "None recorded here";
            }

            const note = L.DomUtil.create("p", "map-legend-note", box);
            // Says "recorded in" on purpose: this is OpenStreetMap's coverage,
            // not a claim about every business that exists.
            note.textContent = scoped
                ? `${total} of ${businessPoints.length} recorded in OpenStreetMap`
                : `${total} recorded in OpenStreetMap`;

            L.DomEvent.disableClickPropagation(box);
            return box;
        };
        legendControl.addTo(map);
    }


    function drawBusinesses(payload) {
    if (!map) return;

    const points = Array.isArray(payload) ? payload : payload?.businesses;

    if (!Array.isArray(points)) {
        throw new Error("/businesses did not return a point array.");
    }

        if (businessLayer) map.removeLayer(businessLayer);
        businessLayer = L.layerGroup().addTo(map);
        const canvas = L.canvas({ pane: "businessPane" });
        const counts = {};
        let drawn = 0;
        businessPoints = [];

        points.forEach((point) => {
            const latitude = Number(point?.latitude);
            const longitude = Number(point?.longitude);

            if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return;

            const group = businessGroup(point?.category);
            counts[group.key] = (counts[group.key] || 0) + 1;

            const pin = L.circleMarker([latitude, longitude], {
                renderer: canvas,
                pane: "businessPane",
                interactive: false,
                radius: 6,
                color: "#ffffff",
                weight: 1.5,
                fillColor: group.color,
                fillOpacity: 0.95
            });

            pin.addTo(businessLayer);
            businessPoints.push({
                geoid: String(point?.geoid ?? ""),
                key: group.key,
                category: String(point?.category || "uncategorised"),
                lat: latitude,
                lng: longitude,
                point,
                group
            });
            drawn += 1;
        });

        refreshLegendForSelection();
    }


    function businessPopupContent(point, group) {
            const popup = document.createElement("div");
            const title = document.createElement("strong");
            title.textContent = String(point.name || "Business");

            const category = document.createElement("div");
            category.className = "popup-facts";
            const raw = String(point.category || "").trim();
            const pretty = raw ? raw.charAt(0).toUpperCase() + raw.slice(1) : "Category not recorded";
            category.textContent = `${pretty} · ${group.label}`;

            // Say where it is in words. The tract code is the reference the
            // figures are published under, not something to lead with.
            const where = document.createElement("div");
            where.className = "popup-group";
            const names = placeNamesByGeoid.get(String(point?.geoid ?? "")) || [];
            where.textContent = names.length
                ? `in ${names.slice(0, 2).join(" · ")}`
                : "Silver Spring, Maryland";

            popup.append(title, category, where);
            return popup;
    }

    function initMap() {
        if (typeof L === "undefined") {
            ui.mapFallback.hidden = false;
            ui.layerStatus.textContent = "Map library unavailable; answers can still be requested.";
            return;
        }

        // Open on wider Silver Spring, not Fenton Village alone.
        // scrollWheelZoom off: the map sits mid-page, and scrolling past it
        // otherwise zooms the map instead of the page - which is exactly what
        // a judge will do first. The +/- buttons and double-click still zoom.
        map = L.map("map", { scrollWheelZoom: false })
            .setView([38.9907, -77.0261], 12);

        // Highlighted tracts call bringToFront(), which would otherwise lift
        // the polygon over the business dots and swallow every click on them.
        // Their own pane keeps the dots on top and clickable.
        map.createPane("businessPane");
        map.getPane("businessPane").style.zIndex = 450;

        map.on("click", handleMapClick);

        watchMapSize();

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

    function watchMapSize() {
        const el = document.getElementById("map");
        if (!el || typeof ResizeObserver === "undefined") return;

        // The map card is below the fold on load, so Leaflet can work out the
        // zoom against a container that has not been laid out yet and settle
        // on a view of half the county. A timer can't fix that - we have to
        // wait until the element actually has a size. Once the user has a
        // selection on screen we stop re-framing, so this never fights them.
        let lastWidth = 0;
        new ResizeObserver((entries) => {
            const width = entries[0].contentRect.width;
            if (!width || width === lastWidth) return;
            lastWidth = width;
            if (activeGeoids.size && highlightScope !== "area") {
                if (map) map.invalidateSize();
                return;
            }
            fitToBoundary();
        }).observe(el);
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
                : "ACS 2016-2020 and 2020-2024 · Census TIGER 2024 · OpenStreetMap",
            failures.length ? "warning" : "live"
        );

        ui.layerStatus.textContent = failures.length
            ? "Some map layers could not load; you can still ask a question."
            : "Silver Spring: 19 census tracts and 455 business locations.";
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