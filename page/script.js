"use strict";

/*
 * BRIDGE SETTINGS — ask the owner of answers/ for these existing routes.
 * Leave blank until the route and its response contract are confirmed.
 * Use relative paths if this page is served by the same FastAPI origin.
 * Do not put a Census or OpenAI API key in this file.
 */
const API = {
    answerUrl: "", // Example shape only: "/existing-answer-route"
    mapUrl: ""     // Optional existing GET route returning GeoJSON
};

const elements = {
    heroVisual: document.getElementById("heroVisual"),
    locationCard: document.getElementById("locationCard"),
    form: document.getElementById("questionForm"),
    question: document.getElementById("questionInput"),
    askButton: document.getElementById("askButton"),
    connection: document.getElementById("connectionStatus"),
    answerStatus: document.getElementById("answerStatus"),
    answerContent: document.getElementById("answerContent"),
    interpretation: document.getElementById("answerInterpretation"),
    answer: document.getElementById("answerText"),
    metrics: document.getElementById("answerMetrics"),
    methodology: document.getElementById("answerMethodology"),
    context: document.getElementById("answerContext"),
    sources: document.getElementById("answerSources"),
    map: document.getElementById("mapContainer"),
    mapGeography: document.getElementById("mapGeography"),
    selectedPlace: document.getElementById("selectedPlace"),
    clearSelection: document.getElementById("clearSelection")
};

let selectedGeoid = null;
let currentFeatures = [];

// Keep the original small hero motion, but respect reduced-motion settings.
if (elements.heroVisual && elements.locationCard &&
    !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    elements.heroVisual.addEventListener("mousemove", (event) => {
        const box = elements.heroVisual.getBoundingClientRect();
        const x = (event.clientX - box.left) / box.width;
        const y = (event.clientY - box.top) / box.height;
        elements.locationCard.style.transform = `translate(${x * 8}px, ${y * 8}px)`;
    });
    elements.heroVisual.addEventListener("mouseleave", () => {
        elements.locationCard.style.transform = "translate(0, 0)";
    });
}

document.querySelectorAll(".nav-links a").forEach((link) => {
    link.addEventListener("click", () => {
        document.querySelectorAll(".nav-links a").forEach((item) => item.classList.remove("active"));
        link.classList.add("active");
    });
});

document.querySelectorAll(".suggestion").forEach((button) => {
    button.addEventListener("click", () => {
        elements.question.value = button.dataset.question || "";
        elements.question.focus();
    });
});

elements.clearSelection.addEventListener("click", () => {
    selectedGeoid = null;
    elements.selectedPlace.textContent = "";
    elements.clearSelection.hidden = true;
    renderMap(currentFeatures);
});

function showStatus(message, isError = false) {
    elements.answerContent.hidden = true;
    elements.answerStatus.hidden = false;
    elements.answerStatus.textContent = message;
    elements.answerStatus.classList.toggle("is-error", isError);
}

function setConnection(message, state = "") {
    elements.connection.textContent = message;
    elements.connection.classList.toggle("is-ready", state === "ready");
    elements.connection.classList.toggle("is-error", state === "error");
}

async function fetchJson(url, options = {}) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 25000);
    try {
        const response = await fetch(url, { ...options, signal: controller.signal });
        if (!response.ok) throw new Error(`Server returned HTTP ${response.status}.`);
        const contentType = response.headers.get("content-type") || "";
        if (!contentType.includes("json")) throw new Error("Server response was not JSON.");
        return await response.json();
    } finally {
        window.clearTimeout(timer);
    }
}

/*
 * BACKEND ADAPTER — this is the main integration seam.
 * Edit this function to match the REAL response from answers/.
 * Do not rename server routes or invent a second backend.
 * Expected frontend shape is documented in README.md.
 */
function normalizeAnswer(payload) {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
        throw new Error("Unexpected answer format from backend.");
    }
    return {
        status: payload.status || "ok",
        answer: typeof payload.answer === "string" ? payload.answer : "",
        interpretation: typeof payload.interpretation === "string" ? payload.interpretation : "",
        metrics: Array.isArray(payload.metrics) ? payload.metrics : [],
        methodology: typeof payload.methodology === "string" ? payload.methodology : "",
        geography: typeof payload.geography === "string" ? payload.geography : "",
        vintage: typeof payload.vintage === "string" ? payload.vintage : "",
        sources: Array.isArray(payload.sources) ? payload.sources : [],
        map: payload.map || null
    };
}

function addText(parent, tag, value, className = "") {
    const node = document.createElement(tag);
    node.textContent = String(value);
    if (className) node.className = className;
    parent.appendChild(node);
    return node;
}

function safeSourceUrl(value) {
    if (typeof value !== "string" || !/^https?:\/\//i.test(value.trim())) return null;
    try {
        const url = new URL(value);
        return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
    } catch (_error) {
        return null;
    }
}

function renderAnswer(result) {
    if (result.status === "no_data" || !result.answer.trim()) {
        showStatus(result.answer || "The available data cannot answer this question yet.");
        return;
    }

    elements.answerStatus.hidden = true;
    elements.answerContent.hidden = false;
    elements.interpretation.textContent = result.interpretation ? `Question interpreted as: ${result.interpretation}` : "";
    elements.answer.textContent = result.answer;
    elements.metrics.replaceChildren();
    result.metrics.forEach((metric) => {
        if (!metric || typeof metric !== "object") return;
        const wrapper = document.createElement("div");
        addText(wrapper, "dt", metric.label ?? "Metric");
        addText(wrapper, "dd", metric.value ?? "Not available");
        if (metric.formula) addText(wrapper, "small", `Formula: ${metric.formula}`);
        elements.metrics.appendChild(wrapper);
    });

    elements.methodology.textContent = result.methodology || "No calculation or methodology was returned.";
    const contextParts = [result.geography, result.vintage && `Data vintage: ${result.vintage}`].filter(Boolean);
    elements.context.textContent = contextParts.join(" · ") || "Geography and data vintage were not returned.";
    elements.sources.replaceChildren();
    result.sources.forEach((source) => {
        if (!source || typeof source !== "object") return;
        const item = document.createElement("li");
        const href = safeSourceUrl(source.url);
        if (href) {
            const link = addText(item, "a", source.label || source.name || href);
            link.href = href;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
        } else {
            addText(item, "span", source.label || source.name || "Source URL unavailable");
        }
        elements.sources.appendChild(item);
    });
    if (!elements.sources.children.length) {
        addText(elements.sources, "li", "No source links were returned. Treat this answer as unverified.");
    }
    if (result.geography) elements.mapGeography.textContent = result.geography;
    if (result.map) renderMap(result.map);
}

elements.form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const question = elements.question.value.trim();
    if (!question) return;
    if (!API.answerUrl) {
        showStatus("The answer route is not configured. Ask your backend teammate for the existing route and JSON response, then edit the BRIDGE SETTINGS and normalizeAnswer() in script.js.", true);
        return;
    }
    elements.askButton.disabled = true;
    showStatus("Checking the data and preparing an answer…");
    try {
        const payload = await fetchJson(API.answerUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ question, geoid: selectedGeoid })
        });
        renderAnswer(normalizeAnswer(payload));
        setConnection("Connected to the backend.", "ready");
    } catch (error) {
        const detail = error.name === "AbortError" ? "The request timed out." : error.message;
        showStatus(`We could not load an answer. ${detail}`, true);
        setConnection("Backend request failed. Check the route, server, and browser console.", "error");
    } finally {
        elements.askButton.disabled = false;
    }
});

function featuresFrom(value) {
    if (value?.type === "FeatureCollection" && Array.isArray(value.features)) return value.features;
    if (Array.isArray(value?.features)) return value.features;
    if (Array.isArray(value)) return value;
    return [];
}

function coordinatePairs(value, output) {
    if (!Array.isArray(value)) return;
    if (value.length >= 2 && Number.isFinite(value[0]) && Number.isFinite(value[1])) {
        output.push(value);
        return;
    }
    value.forEach((item) => coordinatePairs(item, output));
}

function pathForGeometry(geometry, project) {
    if (!geometry || !["Polygon", "MultiPolygon"].includes(geometry.type)) return "";
    const polygons = geometry.type === "Polygon" ? [geometry.coordinates] : geometry.coordinates;
    if (!Array.isArray(polygons)) return "";
    return polygons.flatMap((polygon) => (Array.isArray(polygon) ? polygon : []).map((ring) => {
        if (!Array.isArray(ring) || ring.length < 3) return "";
        return ring.map((pair, index) => {
            if (!Array.isArray(pair) || !Number.isFinite(pair[0]) || !Number.isFinite(pair[1])) return "";
            const [x, y] = project(pair);
            return `${index === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
        }).join(" ") + " Z";
    })).join(" ");
}

function renderMap(input) {
    const features = featuresFrom(input);
    currentFeatures = features;
    const pairs = [];
    features.forEach((feature) => coordinatePairs(feature?.geometry?.coordinates, pairs));
    if (!pairs.length) {
        elements.map.replaceChildren();
        addText(elements.map, "p", "No map geometry was returned by the backend.", "map-placeholder");
        elements.map.setAttribute("aria-label", "No map geometry available");
        return;
    }
    let minLon = Infinity, maxLon = -Infinity, minLat = Infinity, maxLat = -Infinity;
    pairs.forEach(([lon, lat]) => {
        minLon = Math.min(minLon, lon);
        maxLon = Math.max(maxLon, lon);
        minLat = Math.min(minLat, lat);
        maxLat = Math.max(maxLat, lat);
    });
    const centerLon = (minLon + maxLon) / 2, centerLat = (minLat + maxLat) / 2;
    const scale = Math.min(930 / Math.max(maxLon - minLon, .001), 550 / Math.max(maxLat - minLat, .001));
    const project = (pair) => [500 + (pair[0] - centerLon) * scale, 310 - (pair[1] - centerLat) * scale];
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 1000 620");
    svg.setAttribute("aria-label", "CivicLens map with selectable census tracts");

    features.forEach((feature) => {
        const geometry = feature?.geometry;
        const properties = feature?.properties || {};
        if (!geometry || !["Polygon", "MultiPolygon"].includes(geometry.type)) return;
        const path = document.createElementNS(ns, "path");
        path.setAttribute("d", pathForGeometry(geometry, project));
        path.setAttribute("fill-rule", "evenodd");
        path.setAttribute("class", `map-tract${String(properties.geoid) === selectedGeoid ? " is-selected" : ""}`);
        const label = properties.tract_name || properties.name || properties.geoid || "Census tract";
        path.setAttribute("aria-label", String(label));
        const title = document.createElementNS(ns, "title");
        title.textContent = String(label);
        path.appendChild(title);
        if (properties.geoid !== undefined && properties.geoid !== null) {
            path.setAttribute("tabindex", "0");
            const select = () => {
                selectedGeoid = String(properties.geoid); // GEOIDs must retain leading zeros.
                elements.selectedPlace.textContent = `Selected tract: ${label} (${selectedGeoid})`;
                elements.clearSelection.hidden = false;
                renderMap(currentFeatures);
            };
            path.addEventListener("click", select);
            path.addEventListener("keydown", (event) => {
                if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(); }
            });
        }
        svg.appendChild(path);
    });

    features.forEach((feature) => {
        if (feature?.geometry?.type !== "Point") return;
        const pair = feature.geometry.coordinates;
        if (!Array.isArray(pair) || !Number.isFinite(pair[0]) || !Number.isFinite(pair[1])) return;
        const [x, y] = project(pair);
        const point = document.createElementNS(ns, "circle");
        const kind = feature.properties?.layer === "transit" ? "transit" : "business";
        point.setAttribute("class", `map-point-${kind}`);
        point.setAttribute("cx", x.toFixed(2));
        point.setAttribute("cy", y.toFixed(2));
        point.setAttribute("r", "5");
        const title = document.createElementNS(ns, "title");
        title.textContent = String(feature.properties?.name || kind);
        point.appendChild(title);
        svg.appendChild(point);
    });
    elements.map.replaceChildren(svg);
    elements.map.setAttribute("aria-label", "CivicLens map with selectable census tracts and point locations");
}

if (API.answerUrl) setConnection("Answer route configured. Submit a question to test it.", "ready");
if (API.mapUrl) {
    fetchJson(API.mapUrl).then(renderMap).catch((error) => {
        setConnection(`Map could not load: ${error.message}`, "error");
    });
}
