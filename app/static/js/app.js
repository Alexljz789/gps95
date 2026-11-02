/* GPS95 front-end: Windows 95 shell, Leaflet map, satellite views. */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const SYS_ORDER = ["GPS", "GLO", "BDS", "GAL", "QZSS", "NAVIC", "SBAS", "IMES", "UNKNOWN"];
  const SYS_META = {
    GPS: { zh: "GPS", color: "#000080", full: "全球定位系统", country: "美国", orbit: "MEO" },
    GLO: { zh: "格洛纳斯", color: "#006400", full: "GLONASS", country: "俄罗斯", orbit: "MEO" },
    BDS: { zh: "北斗", color: "#a00000", full: "BeiDou", country: "中国", orbit: "MEO/IGSO/GEO" },
    GAL: { zh: "伽利略", color: "#6a0dad", full: "Galileo", country: "欧盟", orbit: "MEO" },
    QZSS: { zh: "准天顶", color: "#006a6a", full: "QZSS", country: "日本", orbit: "IGSO/GEO" },
    NAVIC: { zh: "印度导航", color: "#b35c00", full: "NavIC", country: "印度", orbit: "GEO/IGSO" },
    SBAS: { zh: "SBAS", color: "#7a6a00", full: "星基增强", country: "多国", orbit: "GEO" },
    IMES: { zh: "IMES", color: "#555555", full: "室内定位", country: "日本", orbit: "室内" },
    UNKNOWN: { zh: "未知", color: "#666666", full: "未知系统", country: "-", orbit: "-" },
  };

  let S = null;
  let map = null;
  let tileLayer = null;
  let marker = null;
  let acc1 = null;
  let acc2 = null;
  let trailLine = null;
  let cone = null;
  let follow = true;
  let trailOn = true;
  let accuracyOn = true;
  let coneOn = true;
  let offline = false;
  let theme = "classic";
  let selectedKey = null;
  let currentTab = "list";
  let hasCentered = false;
  let lastFixKey = "";
  let lastSatRender = 0;
  let lastMessageTs = 0;
  let eventSource = null;
  let pollTimer = null;
  let toastTimer = null;
  let prefetchTimer = null;
  let clockTimer = null;
  let bootDialogShown = false;

  // ------------------------------------------------------------------
  // formatting helpers
  function num(value, digits = 0) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
    return Number(value).toFixed(digits);
  }

  function fmtLat(lat, fmt) {
    if (lat === null || lat === undefined) return "--";
    const hemi = lat >= 0 ? "N" : "S";
    return fmtCoord(Math.abs(lat), hemi, fmt, 2);
  }

  function fmtLon(lon, fmt) {
    if (lon === null || lon === undefined) return "--";
    const hemi = lon >= 0 ? "E" : "W";
    return fmtCoord(Math.abs(lon), hemi, fmt, 3);
  }

  function fmtCoord(value, hemi, fmt, degDigits) {
    if (fmt === "dms") {
      const deg = Math.floor(value);
      const minFloat = (value - deg) * 60;
      const min = Math.floor(minFloat);
      const sec = (minFloat - min) * 60;
      return `${String(deg).padStart(degDigits, "0")}°${String(min).padStart(2, "0")}'${sec.toFixed(1).padStart(4, "0")}"${hemi}`;
    }
    return `${value.toFixed(6)}°${hemi}`;
  }

  function fmtSpeed(mps) {
    if (mps === null || mps === undefined) return "--";
    const v = S && S.config && S.config.units === "imperial" ? mps * 2.236936 : mps * 3.6;
    return v.toFixed(1);
  }

  function fmtAlt(m) {
    if (m === null || m === undefined) return "--";
    const v = S && S.config && S.config.units === "imperial" ? m * 3.28084 : m;
    return v.toFixed(0);
  }

  function fmtAge(sec) {
    if (sec === null || sec === undefined) return "--";
    if (sec < 1) return "刚刚";
    if (sec < 60) return `${Math.round(sec)} 秒前`;
    return `${Math.round(sec / 60)} 分前`;
  }

  function maidenhead(lat, lon) {
    if (lat === null || lon === null) return "--";
    const A = "ABCDEFGHIJKLMNOPQRSTUVWX";
    let lo = lon + 180, la = lat + 90;
    const f1 = Math.floor(lo / 20), f2 = Math.floor(la / 10);
    lo -= f1 * 20; la -= f2 * 10;
    const s1 = Math.floor(lo / 2), s2 = Math.floor(la / 1);
    lo -= s1 * 2; la -= s2 * 1;
    const t1 = Math.max(0, Math.min(23, Math.floor(lo * 12)));
    const t2 = Math.max(0, Math.min(23, Math.floor(la * 24)));
    return `${A[f1]}${A[f2]}${s1}${s2}${A[t1]}${A[t2]}`.toUpperCase();
  }

  function esc(text) {
    return String(text ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function systemMeta(sys) { return SYS_META[sys] || SYS_META.UNKNOWN; }

  function seriesShort(sat) {
    const series = String(sat.series || "");
    if (sat.system === "GPS") return series.includes("III") ? "III" : "II/III";
    if (sat.system === "GLO") return "M/K";
    if (sat.system === "BDS") return series.includes("3") ? "BDS-3" : "BDS-2";
    if (sat.system === "GAL") return "FOC";
    if (sat.system === "QZSS") return "IGSO";
    if (sat.system === "SBAS") return "GEO";
    if (sat.system === "NAVIC") return "GEO";
    return "--";
  }

  function toast(text, kind = "", ms = 2600) {
    const layer = $("#toast-layer");
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.textContent = text;
    layer.appendChild(el);
    while (layer.children.length > 3) layer.removeChild(layer.firstChild);
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.remove(), ms);
  }

  function setMessage(text) {
    $("#status-message").textContent = text;
  }

  // ------------------------------------------------------------------
  // data connection
  function connect() {
    if (eventSource) eventSource.close();
    try {
      eventSource = new EventSource("/api/stream");
      eventSource.addEventListener("state", (ev) => {
        try { applyState(JSON.parse(ev.data)); } catch (e) { console.error(e); }
      });
      eventSource.onopen = () => {
        $("#status-led").className = "led small ok";
        setMessage("已连接 GPS95 服务");
      };
      eventSource.onerror = () => {
        $("#status-led").className = "led small bad";
        setMessage("与 GPS95 服务的连接中断，正在重连…");
        if (!pollTimer) startPolling();
      };
    } catch (e) {
      startPolling();
    }
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(async () => {
      try {
        const r = await fetch("/api/state", { cache: "no-store" });
        if (r.ok) applyState(await r.json());
      } catch (e) { /* keep trying */ }
    }, 1000);
  }

  async function api(url, opts = {}) {
    const res = await fetch(url, {
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    let body = null;
    try { body = await res.json(); } catch (e) { body = {}; }
    if (!res.ok) throw new Error(body.error || body.message || `HTTP ${res.status}`);
    return body;
  }

  // ------------------------------------------------------------------
  // map
  function initMap() {
    if (typeof L === "undefined") {
      $("#map").innerHTML = '<div style="padding:12px;font-size:12px">地图库加载失败。请检查 /static/vendor/leaflet。</div>';
      return;
    }
    map = L.map("map", {
      zoomControl: false,
      attributionControl: false,
      minZoom: 3,
      maxZoom: 19,
      worldCopyJump: true,
      preferCanvas: true,
      zoomSnap: 0.5,
      wheelPxPerZoomLevel: 90,
    });
    map.setView([51.5074, -0.1278], 15);
    tileLayer = L.tileLayer(tileUrl(), {
      minZoom: 3, maxZoom: 19, tileSize: 256,
      errorTileUrl: "/static/img/nodata.png",
      attribution: "&copy; OpenStreetMap contributors",
      crossOrigin: false,
    }).addTo(map);
    map.on("dragstart", () => { if (follow) setFollow(false); });
    map.on("zoomstart", () => { /* user zoom keeps position */ });
    map.on("click", () => { /* clicking the map does not disable follow */ });
    const cross = document.createElement("div");
    cross.className = "map-crosshair";
    $("#map").appendChild(cross);
    map.on("resize", () => map.invalidateSize());
    setTimeout(() => map.invalidateSize(), 300);

    const icon = L.divIcon({
      className: "",
      iconSize: [34, 34],
      iconAnchor: [17, 17],
      html: '<div class="gps-marker" id="gps-marker"><div class="halo"></div><div class="arrow" id="gps-arrow"></div></div>',
    });
    marker = L.marker([51.5074, -0.1278], { icon, keyboard: false, interactive: false }).addTo(map);
    marker.setOpacity(0.35);

    acc1 = L.circle([51.5074, -0.1278], {
      radius: 8, color: "#004080", weight: 1.2, opacity: 0.9,
      fillColor: "#0050a0", fillOpacity: 0.12, interactive: false,
    }).addTo(map);
    acc2 = L.circle([51.5074, -0.1278], {
      radius: 16, color: "#a00000", weight: 1, opacity: 0.8,
      dashArray: "5,4", fillColor: "#a00000", fillOpacity: 0.05, interactive: false,
    }).addTo(map);
    acc1.setStyle({ opacity: 0 });
    acc2.setStyle({ opacity: 0 });

    trailLine = L.polyline([], {
      color: "#000080", weight: 2, opacity: 0.75, dashArray: "2,3", interactive: false,
    }).addTo(map);
    cone = L.polygon([], {
      color: "#000080", weight: 0, fillColor: "#0000a0", fillOpacity: 0.12, interactive: false,
    }).addTo(map);
    cone.setStyle({ opacity: 0 });

    window.addEventListener("resize", () => setTimeout(() => map.invalidateSize(), 120));
    setTimeout(() => map.invalidateSize(), 500);
  }

  function tileUrl() {
    return `/tiles/{z}/{x}/{y}.png?offline=${offline ? 1 : 0}`;
  }

  function setOffline(value) {
    offline = !!value;
    if (S && S.config) { S.config.map = S.config.map || {}; S.config.map.offline = offline; }
    if (tileLayer) tileLayer.setUrl(tileUrl(), false);
    $("#btn-offline").classList.toggle("active", offline);
    $("#btn-tool-cache").classList.toggle("active", offline);
    $("#map-sub").textContent = offline ? "Esri 街道 · 离线缓存" : "Esri 街道 · 在线";
    setMessage(offline ? "离线地图：仅使用本地缓存" : "在线地图：自动缓存已浏览区域");
    saveConfig({ map: { offline } });
  }

  function setFollow(value) {
    follow = !!value;
    if (S && S.config) S.config.follow = follow;
    $("#btn-follow").classList.toggle("active", follow);
    $("#btn-tool-follow").classList.toggle("active", follow);
    $("#map-lock").classList.toggle("show", follow);
    if (follow) centerMap(true);
    saveConfig({ follow });
  }

  function setTrail(value) {
    trailOn = !!value;
    if (S && S.config) S.config.trail = trailOn;
    $("#btn-trail").classList.toggle("active", trailOn);
    $("#btn-tool-trail").classList.toggle("active", trailOn);
    if (trailLine) trailLine.setStyle({ opacity: trailOn ? 0.75 : 0 });
    saveConfig({ trail: trailOn });
  }

  function setAccuracy(value) {
    accuracyOn = !!value;
    if (S && S.config) S.config.accuracy = accuracyOn;
    if (acc1) acc1.setStyle({ opacity: accuracyOn ? 0.9 : 0 });
    if (acc2) acc2.setStyle({ opacity: accuracyOn ? 0.8 : 0 });
    saveConfig({ accuracy: accuracyOn });
  }

  function setCone(value) {
    coneOn = !!value;
    if (S && S.config) S.config.heading_cone = coneOn;
    if (cone) cone.setStyle({ opacity: coneOn ? 1 : 0 });
    saveConfig({ heading_cone: coneOn });
  }

  function centerMap(force) {
    if (!map || !S || S.fix.lat === null || S.fix.lon === null) {
      if (force) toast("还没有有效定位，无法居中", "error");
      return;
    }
    const zoom = Math.max(map.getZoom(), 15);
    map.setView([S.fix.lat, S.fix.lon], zoom, { animate: false });
    hasCentered = true;
  }

  function updateMap() {
    if (!map || !S) return;
    const fix = S.fix;
    const hasFix = fix.lat !== null && fix.lon !== null && fix.mode >= 2;
    const lat = fix.lat ?? fix.last_fix_lat;
    const lon = fix.lon ?? fix.last_fix_lon;
    if (lat === null || lon === null) return;
    const latlng = [lat, lon];

    marker.setLatLng(latlng);
    marker.setOpacity(hasFix ? 1 : 0.35);
    const el = $("#gps-arrow");
    if (el) el.style.transform = `rotate(${fix.track || 0}deg)`;

    if (hasFix) {
      const r1 = Math.max(2, Number(fix.acc1 || 8));
      const r2 = Math.max(r1 + 2, Number(fix.acc2 || r1 * 1.96));
      acc1.setLatLng(latlng).setRadius(r1);
      acc2.setLatLng(latlng).setRadius(r2);
      acc1.setStyle({ opacity: accuracyOn ? 0.9 : 0 });
      acc2.setStyle({ opacity: accuracyOn ? 0.8 : 0 });
    } else {
      acc1.setStyle({ opacity: 0 });
      acc2.setStyle({ opacity: 0 });
    }

    if (S.track && S.track.length > 1) {
      trailLine.setLatLngs(S.track.map((p) => [p[0], p[1]]));
      trailLine.setStyle({ opacity: trailOn ? 0.75 : 0 });
    } else {
      trailLine.setLatLngs([]);
    }

    if (hasFix && fix.track !== null && coneOn) {
      const pts = [latlng];
      const dist = Math.max(15, Math.min(80, (fix.speed || 0) * 20 + 20));
      for (let d = -22; d <= 22; d += 11) {
        pts.push(destination(lat, lon, fix.track + d, dist));
      }
      cone.setLatLngs(pts);
      cone.setStyle({ opacity: 1 });
    } else {
      cone.setStyle({ opacity: 0 });
    }

    if (follow && hasFix) {
      const key = `${lat.toFixed(5)},${lon.toFixed(5)}`;
      if (key !== lastFixKey || !hasCentered) {
        lastFixKey = key;
        if (!hasCentered) {
          map.setView(latlng, Math.max(map.getZoom(), 15), { animate: false });
          hasCentered = true;
        } else {
          map.panTo(latlng, { animate: true, duration: 0.4 });
        }
      }
    }
  }

  function destination(lat, lon, bearing, distance) {
    const R = 6378137;
    const br = bearing * Math.PI / 180;
    const p1 = lat * Math.PI / 180;
    const l1 = lon * Math.PI / 180;
    const p2 = Math.asin(Math.sin(p1) * Math.cos(distance / R) +
      Math.cos(p1) * Math.sin(distance / R) * Math.cos(br));
    const l2 = l1 + Math.atan2(Math.sin(br) * Math.sin(distance / R) * Math.cos(p1),
      Math.cos(distance / R) - Math.sin(p1) * Math.sin(p2));
    return [p2 * 180 / Math.PI, l2 * 180 / Math.PI];
  }

  // ------------------------------------------------------------------
  // satellite rendering
  function sortSats(sats) {
    return [...sats].sort((a, b) => {
      if (a.used !== b.used) return a.used ? -1 : 1;
      const sa = SYS_ORDER.indexOf(a.system), sb = SYS_ORDER.indexOf(b.system);
      if (sa !== sb) return sa - sb;
      return a.prn - b.prn;
    });
  }

  function satKey(sat) { return `${sat.system}-${sat.prn}`; }

  function renderSats(force = false) {
    if (!S) return;
    const sats = sortSats(S.sats || []);
    const tbody = $("#sat-tbody");
    const now = Date.now();
    if (!force && now - lastSatRender < 800 && tbody.children.length && sats.length) return;
    lastSatRender = now;
    const frag = document.createDocumentFragment();
    for (const sat of sats) {
      const meta = systemMeta(sat.system);
      const tr = document.createElement("tr");
      tr.dataset.key = satKey(sat);
      tr.className = `${sat.used ? "used" : "unused"}${satKey(sat) === selectedKey ? " selected" : ""}`;
      const snr = sat.snr == null ? 0 : Math.min(99, Math.max(0, sat.snr));
      const snrColor = snr >= 35 ? "#008000" : snr >= 20 ? "#808000" : "#a00000";
      tr.innerHTML = `
        <td><span class="sys-tag" style="color:${meta.color}"><i style="background:${meta.color}"></i>${sat.system === "GLO" ? "GLO" : sat.system}</span></td>
        <td>${sat.prn}</td>
        <td class="series">${seriesShort(sat)}</td>
        <td>${sat.el == null ? "--" : sat.el.toFixed(0) + "°"}</td>
        <td>${sat.az == null ? "--" : sat.az.toFixed(0) + "°"}</td>
        <td><div class="snr-wrap"><div class="snr-bar"><div class="snr-fill" style="width:${snr}%;background:${snrColor}"></div></div><span class="snr-num">${sat.snr == null ? "--" : sat.snr.toFixed(0)}</span></div></td>
        <td><span class="use-mark ${sat.used ? "yes" : ""}">${sat.used ? "●" : "·"}</span></td>`;
      frag.appendChild(tr);
    }
    tbody.replaceChildren(frag);
    $("#sat-empty").classList.toggle("show", sats.length === 0);
    $("#sat-summary").textContent = `${S.total_in_view} 在视 / ${S.total_used} 参与`;
    renderChips();
    renderDetail(sats);
    if (currentTab === "sky") renderSky();
    if (currentTab === "signal") renderSignal();
  }

  function renderChips() {
    const el = $("#const-chips");
    if (!S) return;
    const counts = S.counts || {};
    const parts = [];
    for (const sys of ["GPS", "GLO", "BDS", "GAL", "QZSS", "SBAS"]) {
      const count = counts[sys] || 0;
      if (count === 0 && !["GPS", "GLO", "BDS"].includes(sys)) continue;
      const meta = systemMeta(sys);
      parts.push(`<span class="chip" title="${esc(meta.full)}"><i style="background:${meta.color}"></i>${meta.zh}<b>${count}</b></span>`);
    }
    el.innerHTML = parts.join("");
  }

  function renderDetail(sats) {
    const el = $("#sat-detail");
    if (!selectedKey) {
      if (S && S.total_in_view < 4) {
        el.innerHTML = `<span style="color:#a00000">在视仅 ${S.total_in_view} 颗卫星</span> · 请检查天线/天空视野，或点击“修复 GPS”`;
      } else {
        el.textContent = "点击卫星查看星座、系列与信号详情";
      }
      return;
    }
    const sat = (sats || S.sats || []).find((s) => satKey(s) === selectedKey);
    if (!sat) {
      selectedKey = null;
      el.textContent = "点击卫星查看星座、系列与信号详情";
      return;
    }
    const meta = systemMeta(sat.system);
    const used = sat.used ? "参与定位" : "仅在视";
    el.innerHTML = `<b style="color:${meta.color}">${meta.zh} ${sat.prn}</b> · ${esc(sat.series || meta.full)} · ${meta.country} / ${meta.orbit}` +
      ` · 仰角 ${sat.el == null ? "--" : sat.el.toFixed(0) + "°"} 方位 ${sat.az == null ? "--" : sat.az.toFixed(0) + "°"}` +
      ` · 信噪比 ${sat.snr == null ? "--" : sat.snr.toFixed(0)} dBHz · ${used}`;
  }

  function selectSat(key) {
    selectedKey = selectedKey === key ? null : key;
    renderSats(true);
    if (currentTab === "sky") renderSky();
  }

  // ------------------------------------------------------------------
  // canvas views
  function fitCanvas(canvas) {
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, canvas.clientWidth);
    const h = Math.max(1, canvas.clientHeight);
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return { ctx, w, h };
  }

  function renderSky() {
    const canvas = $("#sky-canvas");
    if (!canvas || !S || canvas.clientWidth < 10) return;
    const { ctx, w, h } = fitCanvas(canvas);
    const bg = getComputedStyle(document.body).getPropertyValue("--field").trim() || "#ffffff";
    const fg = getComputedStyle(document.body).getPropertyValue("--text").trim() || "#000000";
    const dim = getComputedStyle(document.body).getPropertyValue("--shadow").trim() || "#808080";
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, w, h);
    const cx = w / 2, cy = h / 2 + 4, R = Math.min(w, h) * 0.42;
    ctx.strokeStyle = dim;
    ctx.lineWidth = 1;
    for (const el of [0, 30, 60]) {
      const r = (90 - el) / 90 * R;
      ctx.beginPath();
      ctx.arc(cx, cy, r, 0, Math.PI * 2);
      ctx.stroke();
    }
    ctx.beginPath();
    ctx.moveTo(cx - R, cy); ctx.lineTo(cx + R, cy);
    ctx.moveTo(cx, cy - R); ctx.lineTo(cx, cy + R);
    ctx.stroke();
    ctx.fillStyle = fg;
    ctx.font = '10px "W95FA", sans-serif';
    ctx.textAlign = "center";
    ctx.fillText("N", cx, cy - R - 4);
    ctx.fillText("S", cx, cy + R + 11);
    ctx.textAlign = "left";
    ctx.fillText("E", cx + R + 4, cy + 3);
    ctx.textAlign = "right";
    ctx.fillText("W", cx - R - 4, cy + 3);
    ctx.textAlign = "left";
    ctx.fillStyle = dim;
    ctx.fillText("90°", cx + 3, cy + 12);
    ctx.fillText("0°", cx + R - 18, cy + 12);

    for (const sat of (S.sats || [])) {
      if (sat.el == null || sat.az == null) continue;
      const meta = systemMeta(sat.system);
      const r = (90 - Math.max(0, Math.min(90, sat.el))) / 90 * R;
      const a = sat.az * Math.PI / 180;
      const x = cx + r * Math.sin(a);
      const y = cy - r * Math.cos(a);
      ctx.beginPath();
      ctx.rect(x - 3.5, y - 3.5, 7, 7);
      if (sat.used) {
        ctx.fillStyle = meta.color;
        ctx.fill();
      } else {
        ctx.strokeStyle = meta.color;
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }
      if (`${sat.system}-${sat.prn}` === selectedKey) {
        ctx.strokeStyle = "#ff0000";
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.arc(x, y, 8, 0, Math.PI * 2);
        ctx.stroke();
      }
      ctx.fillStyle = fg;
      ctx.font = '9px "W95FA", sans-serif';
      ctx.fillText(String(sat.prn), x + 5, y - 4);
    }
    renderSkyLegend();
  }

  function renderSkyLegend() {
    const el = $("#sky-legend");
    if (!S) return;
    const used = new Set((S.sats || []).filter((s) => s.used).map((s) => s.system));
    const parts = [];
    for (const sys of ["GPS", "GLO", "BDS", "GAL", "QZSS", "SBAS"]) {
      if (!(S.counts || {})[sys]) continue;
      const meta = systemMeta(sys);
      parts.push(`<span><i style="background:${meta.color}"></i>${meta.zh}${used.has(sys) ? "●" : "○"}</span>`);
    }
    el.innerHTML = parts.join("") || "<span>等待卫星…</span>";
  }

  function renderSignal() {
    const canvas = $("#signal-canvas");
    if (!canvas || !S || canvas.clientWidth < 10) return;
    const { ctx, w, h } = fitCanvas(canvas);
    const bg = getComputedStyle(document.body).getPropertyValue("--field").trim() || "#ffffff";
    const fg = getComputedStyle(document.body).getPropertyValue("--text").trim() || "#000000";
    const dim = getComputedStyle(document.body).getPropertyValue("--shadow").trim() || "#808080";
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, w, h);
    const sats = sortSats(S.sats || []).filter((s) => s.snr != null);
    if (!sats.length) {
      ctx.fillStyle = dim;
      ctx.font = '11px "W95FA", sans-serif';
      ctx.textAlign = "center";
      ctx.fillText("等待信噪比数据…", w / 2, h / 2);
      return;
    }
    const left = 42, right = 10, top = 16, bottom = 20;
    const barW = Math.max(8, (w - left - right) / sats.length - 5);
    const maxH = h - top - bottom;
    ctx.strokeStyle = dim;
    ctx.beginPath();
    ctx.moveTo(left, top); ctx.lineTo(left, top + maxH); ctx.lineTo(w - right, top + maxH);
    ctx.stroke();
    ctx.font = '8px "W95FA", sans-serif';
    ctx.fillStyle = dim;
    ctx.textAlign = "right";
    for (const v of [0, 20, 40, 60]) {
      const y = top + maxH - (v / 60) * maxH;
      ctx.fillText(String(v), left - 4, y + 2);
      ctx.beginPath(); ctx.moveTo(left - 2, y); ctx.lineTo(left, y); ctx.stroke();
    }
    sats.forEach((sat, i) => {
      const meta = systemMeta(sat.system);
      const x = left + 4 + i * ((w - left - right) / sats.length);
      const bh = Math.max(1, Math.min(60, sat.snr) / 60 * maxH);
      ctx.fillStyle = sat.used ? meta.color : dim;
      ctx.fillRect(x, top + maxH - bh, barW, bh);
      ctx.fillStyle = fg;
      ctx.font = '8px "W95FA", sans-serif';
      ctx.textAlign = "center";
      ctx.fillText(String(sat.prn), x + barW / 2, top + maxH + 10);
    });
  }

  // ------------------------------------------------------------------
  // status / controls
  function applyState(next) {
    S = next;
    if (!map) initMap();
    applyConfigUI();
    renderStatus();
    renderSats();
    updateMap();
    renderClock();
    if (!bootDialogShown) {
      bootDialogShown = true;
      const want = new URLSearchParams(location.search).get("dialog");
      if (want === "settings") setTimeout(openSettings, 200);
      else if (want === "cache") setTimeout(openCacheDialog, 200);
      else if (want === "diagnostics") setTimeout(openDiagnostics, 200);
      else if (want === "help") setTimeout(showHelp, 200);
      else if (want === "about") setTimeout(showAbout, 200);
    }
  }

  function applyConfigUI() {
    if (!S || !S.config) return;
    const cfg = S.config;
    if (cfg.theme && cfg.theme !== theme) {
      theme = cfg.theme;
      document.body.classList.toggle("theme-night", theme === "night");
    }
    if (typeof cfg.follow === "boolean") {
      follow = cfg.follow;
      $("#btn-follow").classList.toggle("active", follow);
      $("#btn-tool-follow").classList.toggle("active", follow);
      $("#map-lock").classList.toggle("show", follow);
    }
    if (typeof cfg.trail === "boolean") {
      trailOn = cfg.trail;
      $("#btn-trail").classList.toggle("active", trailOn);
      $("#btn-tool-trail").classList.toggle("active", trailOn);
    }
    if (typeof cfg.accuracy === "boolean") {
      accuracyOn = cfg.accuracy;
      if (acc1) acc1.setStyle({ opacity: accuracyOn ? 0.9 : 0 });
      if (acc2) acc2.setStyle({ opacity: accuracyOn ? 0.8 : 0 });
    }
    if (typeof cfg.heading_cone === "boolean") coneOn = cfg.heading_cone;
    if (cfg.map && typeof cfg.map.offline === "boolean" && cfg.map.offline !== offline) {
      offline = cfg.map.offline;
      if (tileLayer) tileLayer.setUrl(tileUrl(), false);
      $("#btn-offline").classList.toggle("active", offline);
      $("#btn-tool-cache").classList.toggle("active", offline);
      $("#map-sub").textContent = offline ? "Esri 街道 · 离线缓存" : "Esri 街道 · 在线";
    }
  }

  function renderStatus() {
    if (!S) return;
    const fix = S.fix;
    const hasFix = fix.mode >= 2 && fix.lat !== null;
    const badge = $("#fix-badge-text");
    const led = $("#fix-led");
    if (S.source.status === "error") {
      badge.textContent = "数据源错误";
      led.className = "led bad";
    } else if (hasFix && fix.mode >= 3) {
      badge.textContent = "3D 定位 · " + (S.total_used || 0) + " 星";
      led.className = "led ok";
    } else if (hasFix) {
      badge.textContent = "2D 定位";
      led.className = "led warn";
    } else {
      badge.textContent = "搜索卫星…";
      led.className = "led search";
    }

    $("#title-sub").textContent = `uConsole / GP-02 · ${S.bridge.mode ? gnssModeName(S.bridge.mode) : "GPS+BDS+GLONASS"}`;
    const demoBadge = $("#demo-badge");
    if (demoBadge) demoBadge.style.display = S.demo ? "inline-block" : "none";
    $("#control-sub").textContent = hasFix
      ? `${fix.mode_text} · ${S.total_used} 颗参与`
      : (S.source.status === "error" ? "数据源错误" : "等待定位");

    $("#v-lat").textContent = fmtLat(fix.lat, coordFormat());
    $("#v-lon").textContent = fmtLon(fix.lon, coordFormat());
    $("#v-grid").textContent = maidenhead(fix.lat, fix.lon);
    $("#ov-lat").textContent = fmtLat(fix.lat, "dd");
    $("#ov-lon").textContent = fmtLon(fix.lon, "dd");
    $("#ov-acc").textContent = fix.acc1 ? `±${num(fix.acc1, 1)} m` : "--";
    $("#v-speed").textContent = fmtSpeed(fix.speed);
    $("#v-alt").textContent = fmtAlt(fix.alt);
    $("#v-course").textContent = fix.track == null ? "--" : num(fix.track, 0);
    $("#v-hdop").textContent = fix.hdop == null ? "--" : num(fix.hdop, 2);
    $("#v-acc").textContent = fix.acc1 ? `±${num(fix.acc1, 1)} m` : "--";
    const accPct = fix.acc1 ? Math.max(4, Math.min(100, 100 - fix.acc1 * 1.6)) : 0;
    $("#acc-fill").style.width = `${accPct}%`;

    $("#v-source").textContent = sourceLabel(S.source.name, S.source.status);
    $("#v-sats").textContent = `卫星 ${S.total_in_view}/${S.total_used}`;
    $("#v-time").textContent = fix.time ? new Date(fix.time).toLocaleTimeString("zh-CN", { hour12: false }) : "--:--:--";
    const sLed = $("#source-led");
    sLed.className = `source-led ${S.source.status === "ok" ? "ok" : S.source.status === "degraded" ? "warn" : "bad"}`;
    sLed.title = `数据源：${S.source.name} · ${S.source.detail || ""}`;

    $("#status-source").innerHTML = `<span class="led small ${S.source.status === "ok" ? "ok" : S.source.status === "degraded" ? "warn" : "bad"}"></span> 数据源: ${esc(sourceLabel(S.source.name, S.source.status))}`;
    $("#status-fix").textContent = `定位: ${fix.quality_text}${fix.age != null ? " · " + fmtAge(fix.age) : ""}`;
    $("#status-sats").textContent = `卫星: ${S.total_in_view} 在视 / ${S.total_used} 参与`;
    const cache = S.cache || {};
    $("#status-cache").textContent = `缓存: ${cache.files || 0} 块 / ${cache.mb || 0} MB`;
    $("#status-clock").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    $("#menu-clock").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    $("#map-cache-state").textContent = `缓存 ${cache.files || 0} 块 · ${cache.mb || 0} MB${offline ? " · 离线" : ""}`;
    $("#map-sub").textContent = offline ? "Esri 街道 · 离线缓存" : "Esri 街道 · 在线";

    if (S.messages && S.messages.length) {
      const last = S.messages[S.messages.length - 1];
      if (last.ts > lastMessageTs) {
        lastMessageTs = last.ts;
        setMessage(`${last.time} ${last.text}`);
      }
    }
  }

  function coordFormat() {
    return S && S.config && S.config.coord_format === "dms" ? "dms" : "dd";
  }

  function gnssModeName(mode) {
    return ({ 1: "GPS", 2: "北斗", 3: "GPS+北斗", 4: "GLONASS", 5: "GPS+GLONASS", 6: "北斗+GLONASS", 7: "GPS+北斗+GLONASS" })[mode] || "多星座";
  }

  function sourceLabel(name, status) {
    const map = { gpsd: "GPSD", bridge: "硬件桥", serial: "硬件桥", "serial-direct": "硬件串口", demo: "演示", none: "无", starting: "启动中" };
    return `${map[name] || name}${status === "degraded" ? "（降级）" : status === "error" ? "（错误）" : ""}`;
  }

  function renderClock() {
    const now = new Date();
    $("#status-clock").textContent = now.toLocaleTimeString("zh-CN", { hour12: false });
    $("#menu-clock").textContent = now.toLocaleTimeString("zh-CN", { hour12: false });
  }

  // ------------------------------------------------------------------
  // config
  let saveTimer = null;
  function saveConfig(patch) {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(async () => {
      try {
        const res = await api("/api/config", { method: "POST", body: JSON.stringify(patch) });
        if (S && res.config) S.config = res.config;
      } catch (e) {
        toast(e.message, "error");
      }
    }, 250);
  }

  // ------------------------------------------------------------------
  // menus
  function closeMenu() {
    $("#menu-layer").innerHTML = "";
    $$(".menu-item").forEach((el) => el.classList.remove("open"));
  }

  function openMenu(name, anchor) {
    closeMenu();
    const items = menuItems(name);
    const pop = document.createElement("div");
    pop.className = "menu-popup";
    for (const item of items) {
      if (item.sep) {
        const sep = document.createElement("div");
        sep.className = "sep";
        pop.appendChild(sep);
        continue;
      }
      const el = document.createElement("div");
      el.className = `mi${item.disabled ? " disabled" : ""}`;
      el.innerHTML = `<span>${esc(item.label)}</span><span class="sc">${esc(item.short || "")}</span>`;
      el.addEventListener("click", () => { closeMenu(); item.action && item.action(); });
      pop.appendChild(el);
    }
    $("#menu-layer").appendChild(pop);
    const r = anchor.getBoundingClientRect();
    const pr = pop.getBoundingClientRect();
    pop.style.left = `${Math.min(window.innerWidth - pr.width - 4, r.left)}px`;
    pop.style.top = `${r.bottom}px`;
    anchor.classList.add("open");
  }

  function menuItems(name) {
    if (name === "file") {
      return [
        { label: "导出轨迹 (GPX)", short: "", action: () => { window.location.href = "/api/track.gpx"; } },
        { label: "导出/查看 NMEA", short: "", action: showNmea },
        { sep: true },
        { label: "退出 GPS95", short: "Alt+F4", action: confirmExit },
      ];
    }
    if (name === "view") {
      return [
        { label: `${follow ? "✓ " : ""}跟随当前位置`, short: "C", action: () => setFollow(true) },
        { label: `${trailOn ? "✓ " : ""}显示轨迹`, short: "T", action: () => setTrail(!trailOn) },
        { label: `${accuracyOn ? "✓ " : ""}显示精度圈`, short: "A", action: () => setAccuracy(!accuracyOn) },
        { label: `${coneOn ? "✓ " : ""}显示航向锥`, short: "", action: () => setCone(!coneOn) },
        { sep: true },
        { label: `${theme === "night" ? "✓ " : ""}夜间配色`, short: "", action: toggleTheme },
        { label: "全屏", short: "F11", action: toggleFullscreen },
      ];
    }
    if (name === "tools") {
      return [
        { label: "缓存当前地图区域…", short: "", action: openCacheDialog },
        { label: "清除地图缓存…", short: "", action: confirmClearCache },
        { sep: true },
        { label: "修复 GPS（恢复 NMEA）", short: "R", action: repairGps },
        { label: "诊断…", short: "D", action: openDiagnostics },
        { label: "设置…", short: "S", action: openSettings },
      ];
    }
    return [
      { label: "快捷键…", short: "F1", action: showHelp },
      { label: "关于 GPS95…", short: "", action: showAbout },
    ];
  }

  // ------------------------------------------------------------------
  // dialogs
  function openDialog({ title, body, width = 460, buttons = [], onOpen }) {
    const layer = $("#modal-layer");
    layer.innerHTML = "";
    const dlg = document.createElement("div");
    dlg.className = "dialog";
    dlg.style.width = `${width}px`;
    dlg.innerHTML = `
      <div class="title-bar">
        <div class="title-bar-text"><svg class="title-icon"><use href="#i-sat"/></svg>${esc(title)}</div>
        <div class="title-bar-controls"><button aria-label="Close" class="dlg-close"></button></div>
      </div>
      <div class="dialog-body">${body}</div>
      <div class="dialog-foot"></div>`;
    const foot = $(".dialog-foot", dlg);
    for (const b of buttons) {
      const btn = document.createElement("button");
      btn.textContent = b.label;
      if (b.primary) btn.classList.add("primary");
      btn.addEventListener("click", async () => {
        if (b.onClick) {
          const keep = await b.onClick(dlg);
          if (keep === false) return;
        }
        if (b.close !== false) closeDialog();
      });
      foot.appendChild(btn);
    }
    $(".dlg-close", dlg).addEventListener("click", closeDialog);
    layer.appendChild(dlg);
    layer.classList.add("show");
    onOpen && onOpen(dlg);
    const first = dlg.querySelector("input,select,textarea,button.primary");
    if (first) setTimeout(() => first.focus(), 30);
  }

  function closeDialog() {
    $("#modal-layer").classList.remove("show");
    $("#modal-layer").innerHTML = "";
    if (prefetchTimer) { clearInterval(prefetchTimer); prefetchTimer = null; }
  }

  function dialogButton(label, onClick, primary = false) {
    return { label, onClick, primary };
  }

  function showHelp() {
    openDialog({
      title: "GPS95 帮助",
      width: 520,
      body: `
        <h3>键盘快捷键</h3>
        <div class="stat-grid">
          <div class="stat"><span class="k">跟随 / 居中</span><span class="v">C</span></div>
          <div class="stat"><span class="k">轨迹开关</span><span class="v">T</span></div>
          <div class="stat"><span class="k">精度圈开关</span><span class="v">A</span></div>
          <div class="stat"><span class="k">修复 GPS</span><span class="v">R</span></div>
          <div class="stat"><span class="k">缓存地图</span><span class="v">M</span></div>
          <div class="stat"><span class="k">离线地图</span><span class="v">O</span></div>
          <div class="stat"><span class="k">夜间配色</span><span class="v">N</span></div>
          <div class="stat"><span class="k">诊断</span><span class="v">D</span></div>
          <div class="stat"><span class="k">设置</span><span class="v">S</span></div>
          <div class="stat"><span class="k">帮助</span><span class="v">F1</span></div>
          <div class="stat"><span class="k">缩放</span><span class="v">+ / −</span></div>
          <div class="stat"><span class="k">卫星视图</span><span class="v">1 / 2 / 3</span></div>
        </div>
        <h3>关于本机的 GPS 问题</h3>
        <p>uConsole AIO v2 使用 Ai-Thinker GP-02（CASIC）模块。gpsd 的 u-blox 自动配置会把它切换到二进制模式，导致 TPV 有位置但 SKY 没有卫星；gpsd 退出后模块还可能停留在二进制模式，PyGPSClient 直读时只看到乱码。</p>
        <p>GPS95 的硬件桥接服务独占串口，发送 <code>PCAS03</code> / <code>PCAS04,7</code> 恢复 NMEA 并把 GPS、北斗、GLONASS 全部打开，然后把纯 NMEA 通过 PTY 交给 gpsd。gpsd 因此能正常生成 SKY，xgps/cgps 也能正常显示。</p>
        <p>如果仍然没有数据，点击“修复 GPS”：桥接服务会重新配置模块，必要时发送 <code>PCAS10,2</code> 重启。诊断对话框里可以看到桥接、gpsd 和串口状态。</p>
        <div class="hint">提示：GPS 天线需要朝向天空。首次定位或冷启动后可能需要 30 秒到数分钟。</div>`,
      buttons: [dialogButton("确定", null, true)],
    });
  }

  function showAbout() {
    const sys = (S && S.system) || {};
    openDialog({
      title: "关于 GPS95",
      width: 470,
      body: `
        <h3>GPS95 — 卫星导航 1.0</h3>
        <p>为 ClockworkPi uConsole + HackerGadgets AIO v2 设计的 Windows 95 风格 GPS/GNSS 导航仪。</p>
        <div class="stat-grid">
          <div class="stat"><span class="k">主机</span><span class="v">${esc(sys.hostname || "uConsole")}</span></div>
          <div class="stat"><span class="k">内核</span><span class="v">${esc(sys.kernel || "--")}</span></div>
          <div class="stat"><span class="k">GPSD</span><span class="v">${esc(sys.gpsd_version || "--")}</span></div>
          <div class="stat"><span class="k">GPS 模块</span><span class="v">GP-02 / CASIC</span></div>
        </div>
        <p>技术栈：Python 3 / Flask / pyserial / Leaflet / 98.css / W95FA。地图 © OpenStreetMap contributors，按 ODbL 许可使用。</p>
        <p>本软件以 MIT 许可发布。GPSD 与 Leaflet 的许可分别见其项目主页。</p>`,
      buttons: [dialogButton("确定", null, true)],
    });
  }

  function showNmea() {
    openDialog({
      title: "NMEA 监视器",
      width: 560,
      body: `<div class="hint">当前最后一条语句与原始统计。完整日志见诊断对话框。</div>
        <textarea id="nmea-text" readonly style="height:170px"></textarea>
        <div id="nmea-meta" class="hint" style="margin-top:4px"></div>`,
      buttons: [
        dialogButton("刷新", async (dlg) => {
          try {
            const r = await api("/api/nmea");
            $("#nmea-text", dlg).value = r.last || "(无)";
            $("#nmea-meta", dlg).textContent = `总计 ${r.sentences} 条，校验失败 ${r.bad_checksums} 条，来源 ${r.source}`;
          } catch (e) { toast(e.message, "error"); }
          return false;
        }),
        dialogButton("关闭", null, true),
      ],
      onOpen: async (dlg) => {
        try {
          const r = await api("/api/nmea");
          $("#nmea-text", dlg).value = r.last || "(无)";
          $("#nmea-meta", dlg).textContent = `总计 ${r.sentences} 条，校验失败 ${r.bad_checksums} 条，来源 ${r.source}`;
        } catch (e) { /* ignore */ }
      },
    });
  }

  function openSettings() {
    if (!S) return;
    const cfg = S.config || {};
    const mapcfg = cfg.map || {};
    openDialog({
      title: "GPS95 设置",
      width: 540,
      body: `
        <div class="form-section">GPS 数据源</div>
        <div class="form-row"><label>工作模式</label>
          <select id="set-source">
            <option value="auto">自动（优先 GPSD，失败则直连）</option>
            <option value="gpsd">仅 GPSD</option>
            <option value="serial">仅硬件串口</option>
          </select></div>
        <div class="form-row"><label>星座组合</label>
          <select id="set-gnss">
            <option value="7">GPS + 北斗 + GLONASS（最多卫星）</option>
            <option value="5">GPS + GLONASS</option>
            <option value="3">GPS + 北斗</option>
            <option value="1">仅 GPS</option>
          </select></div>
        <div class="form-row"><label>自动修复</label>
          <div class="check"><input type="checkbox" id="set-autorepair"><label for="set-autorepair">数据超时后自动恢复 GP-02 配置</label></div></div>

        <div class="form-section">显示与地图</div>
        <div class="form-row"><label>单位</label>
          <select id="set-units"><option value="metric">公制（km/h, m）</option><option value="imperial">英制（mph, ft）</option></select></div>
        <div class="form-row"><label>坐标格式</label>
          <select id="set-coord"><option value="dd">十进制度</option><option value="dms">度分秒</option></select></div>
        <div class="form-row"><label>配色</label>
          <select id="set-theme"><option value="classic">Windows 95 经典</option><option value="night">夜间深色</option></select></div>
        <div class="form-row"><label>瓦片地址</label><input type="text" id="set-upstream" value="${esc(mapcfg.upstream || "")}"></div>
        <div class="form-row"><label>缓存上限</label><input type="text" id="set-maxmb" value="${esc(mapcfg.max_cache_mb || 512)}"></div>
        <div class="form-row"><label>地图行为</label>
          <div class="check"><input type="checkbox" id="set-offline"><label for="set-offline">离线模式（只读缓存）</label></div></div>
        <div class="hint">默认瓦片源为 Esri World Street Map（© Esri, HERE, Garmin, OSM contributors）。大量预抓取请使用自己的瓦片服务或 MBTiles；本软件默认限制单次 450 块。</div>`,
      buttons: [
        dialogButton("保存", async (dlg) => {
          const patch = {
            source_mode: $("#set-source", dlg).value,
            gnss_mode: Number($("#set-gnss", dlg).value),
            auto_repair: $("#set-autorepair", dlg).checked,
            units: $("#set-units", dlg).value,
            coord_format: $("#set-coord", dlg).value,
            theme: $("#set-theme", dlg).value,
            map: {
              upstream: $("#set-upstream", dlg).value.trim(),
              max_cache_mb: Number($("#set-maxmb", dlg).value) || 512,
              offline: $("#set-offline", dlg).checked,
            },
          };
          try {
            const res = await api("/api/config", { method: "POST", body: JSON.stringify(patch) });
            if (S) S.config = res.config;
            theme = patch.theme;
            document.body.classList.toggle("theme-night", theme === "night");
            setOffline(patch.map.offline);
            toast("设置已保存", "ok");
          } catch (e) {
            toast(e.message, "error");
            return false;
          }
        }, true),
        dialogButton("取消", null),
      ],
      onOpen: (dlg) => {
        $("#set-source", dlg).value = cfg.source_mode || "auto";
        $("#set-gnss", dlg).value = String(cfg.gnss_mode || 7);
        $("#set-autorepair", dlg).checked = cfg.auto_repair !== false;
        $("#set-units", dlg).value = cfg.units || "metric";
        $("#set-coord", dlg).value = cfg.coord_format || "dd";
        $("#set-theme", dlg).value = cfg.theme || "classic";
        $("#set-offline", dlg).checked = !!mapcfg.offline;
      },
    });
  }

  function openCacheDialog() {
    const bounds = map ? map.getBounds() : null;
    const z = map ? Math.round(map.getZoom()) : 15;
    const z0 = Math.max(3, z - 1), z1 = Math.min(17, z + 2);
    openDialog({
      title: "离线地图缓存",
      width: 520,
      body: `
        <div class="stat-grid" id="cache-stats"><div class="stat"><span class="k">读取中…</span><span class="v">--</span></div></div>
        <div class="form-section">缓存当前视野</div>
        <div class="form-row"><label>缩放级别</label>
          <div class="check"><span>${z0}</span><input type="range" id="cache-z0" min="3" max="18" value="${z0}" style="flex:1"><span id="cache-z0v">${z0}</span></div></div>
        <div class="form-row"><label>到</label>
          <div class="check"><span>${z1}</span><input type="range" id="cache-z1" min="3" max="18" value="${z1}" style="flex:1"><span id="cache-z1v">${z1}</span></div></div>
        <div class="form-row"><label>上限</label><input type="text" id="cache-max" value="450"></div>
        <div class="progress" style="margin:6px 0"><div id="cache-progress"></div></div>
        <div class="hint" id="cache-msg">当前视野大致范围：${bounds ? bounds.toBBoxString().split(",").map((v) => Number(v).toFixed(4)).join(", ") : "--"}</div>`,
      buttons: [
        dialogButton("缓存当前视野", async (dlg) => {
          if (!map) return false;
          const b = map.getBounds();
          const body = {
            south: b.getSouth(), west: b.getWest(), north: b.getNorth(), east: b.getEast(),
            minzoom: Number($("#cache-z0", dlg).value),
            maxzoom: Number($("#cache-z1", dlg).value),
            max_tiles: Number($("#cache-max", dlg).value) || 450,
          };
          try {
            const r = await api("/api/cache/prefetch", { method: "POST", body: JSON.stringify(body) });
            $("#cache-msg", dlg).textContent = r.message;
            startPrefetchPoll(dlg);
          } catch (e) {
            $("#cache-msg", dlg).textContent = e.message;
            toast(e.message, "error");
          }
          return false;
        }, true),
        dialogButton("清除缓存", async () => {
          try {
            const r = await api("/api/cache/clear", { method: "POST", body: "{}" });
            toast(`已清除缓存（${r.stats.files} 块剩余）`, "ok");
            refreshCacheStats();
          } catch (e) { toast(e.message, "error"); }
        }),
        dialogButton("关闭", null),
      ],
      onOpen: async (dlg) => {
        $("#cache-z0", dlg).addEventListener("input", () => { $("#cache-z0v", dlg).textContent = $("#cache-z0", dlg).value; });
        $("#cache-z1", dlg).addEventListener("input", () => { $("#cache-z1v", dlg).textContent = $("#cache-z1", dlg).value; });
        await refreshCacheStats();
        startPrefetchPoll(dlg);
      },
    });
  }

  async function refreshCacheStats() {
    const el = $("#cache-stats");
    if (!el) return;
    try {
      const s = await api("/api/cache/stats");
      el.innerHTML = `
        <div class="stat"><span class="k">已缓存</span><span class="v">${s.files} 块 / ${s.mb} MB</span></div>
        <div class="stat"><span class="k">上限</span><span class="v">${s.max_mb} MB</span></div>
        <div class="stat"><span class="k">命中 / 未命中</span><span class="v">${s.hits} / ${s.misses}</span></div>
        <div class="stat"><span class="k">网络</span><span class="v">${s.online === false ? "离线/受限" : "在线"}</span></div>`;
      if (s.prefetch && s.prefetch.running) {
        $("#cache-msg").textContent = `${s.prefetch.message} ${s.prefetch.done}/${s.prefetch.total}`;
      }
    } catch (e) { /* ignore */ }
  }

  function startPrefetchPoll(dlg) {
    if (prefetchTimer) clearInterval(prefetchTimer);
    prefetchTimer = setInterval(async () => {
      if (!document.body.contains(dlg)) { clearInterval(prefetchTimer); prefetchTimer = null; return; }
      try {
        const s = await api("/api/cache/stats");
        const p = s.prefetch || {};
        if (p.total) {
          $("#cache-progress", dlg).style.width = `${Math.min(100, (p.done / p.total) * 100)}%`;
          $("#cache-msg", dlg).textContent = `${p.message || "缓存中"} ${p.done}/${p.total}${p.errors ? " · 失败 " + p.errors : ""}`;
        }
        if (!p.running) {
          clearInterval(prefetchTimer); prefetchTimer = null;
          refreshCacheStats();
        }
      } catch (e) { /* ignore */ }
    }, 600);
  }

  function confirmClearCache() {
    openDialog({
      title: "确认",
      width: 380,
      body: "<p>确定要删除所有已缓存的地图瓦片吗？离线地图将不可用，直到重新在线浏览或缓存。</p>",
      buttons: [
        dialogButton("清除", async () => {
          try {
            await api("/api/cache/clear", { method: "POST", body: "{}" });
            toast("地图缓存已清除", "ok");
          } catch (e) { toast(e.message, "error"); }
        }, true),
        dialogButton("取消", null),
      ],
    });
  }

  function openDiagnostics() {
    openDialog({
      title: "GPS95 诊断",
      width: 600,
      body: `<div id="diag-stats" class="stat-grid"><div class="stat"><span class="k">读取中…</span><span class="v">--</span></div></div>
        <div class="form-section">日志</div>
        <textarea id="diag-log" readonly style="height:190px"></textarea>
        <div class="hint" id="diag-hint"></div>`,
      buttons: [
        dialogButton("刷新", async (dlg) => { await loadDiagnostics(dlg); return false; }),
        dialogButton("修复 GPS", async () => { await repairGps(); }),
        dialogButton("复制日志", async (dlg) => {
          const text = $("#diag-log", dlg).value;
          try { await navigator.clipboard.writeText(text); toast("日志已复制", "ok"); }
          catch (e) { toast("复制失败，请手动选择文本", "error"); }
          return false;
        }),
        dialogButton("关闭", null),
      ],
      onOpen: (dlg) => loadDiagnostics(dlg),
    });
  }

  async function loadDiagnostics(dlg) {
    try {
      const h = await api("/api/hardware");
      const st = h.system || {};
      const src = h.source || {};
      const br = h.bridge || {};
      const gpsd = src.gpsd || {};
      $("#diag-stats", dlg).innerHTML = `
        <div class="stat"><span class="k">数据源</span><span class="v">${esc(src.active || "--")} / ${esc(src.mode || "--")}</span></div>
        <div class="stat"><span class="k">GPSD</span><span class="v">${esc(st.gpsd || "--")} ${gpsd.connected ? "已连接" : "未连接"}</span></div>
        <div class="stat"><span class="k">桥接服务</span><span class="v">${esc(st.bridge || "--")}</span></div>
        <div class="stat"><span class="k">串口</span><span class="v">${esc(br.serial_port || st.serial || "--")} ${br.serial_ok ? "OK" : "ERR"}</span></div>
        <div class="stat"><span class="k">NMEA</span><span class="v">${br.nmea_count == null ? "--" : br.nmea_count} 条</span></div>
        <div class="stat"><span class="k">最后语句</span><span class="v">${br.last_nmea_age == null ? "--" : br.last_nmea_age + "s 前"}</span></div>
        <div class="stat"><span class="k">温度</span><span class="v">${st.temp == null ? "--" : st.temp + " °C"}</span></div>
        <div class="stat"><span class="k">电池</span><span class="v">${st.battery && st.battery.capacity != null ? st.battery.capacity + "%" : "--"}</span></div>`;
      $("#diag-hint", dlg).textContent = br.serial_error || src.last_error || "";
      const log = await api("/api/log?lines=160");
      $("#diag-log", dlg).value = log.text || "(无日志)";
      $("#diag-log", dlg).scrollTop = $("#diag-log", dlg).scrollHeight;
    } catch (e) {
      toast(e.message, "error");
    }
  }

  function confirmExit() {
    openDialog({
      title: "退出 GPS95",
      width: 360,
      body: "<p>关闭导航窗口？后台 GPS 桥接和 gpsd 服务会继续运行，以便下次快速启动。</p>",
      buttons: [
        dialogButton("退出", () => { window.close(); setTimeout(() => toast("无法关闭窗口，请按 Alt+F4"), 300); }, true),
        dialogButton("取消", null),
      ],
    });
  }

  function toggleTheme() {
    theme = theme === "night" ? "classic" : "night";
    if (S && S.config) S.config.theme = theme;
    document.body.classList.toggle("theme-night", theme === "night");
    saveConfig({ theme });
    setTimeout(() => { if (currentTab === "sky") renderSky(); if (currentTab === "signal") renderSignal(); }, 50);
  }

  function toggleFullscreen() {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen().catch(() => toast("无法进入全屏", "error"));
    } else {
      document.exitFullscreen().catch(() => {});
    }
  }

  // ------------------------------------------------------------------
  // actions
  async function repairGps() {
    try {
      const btn = $("#btn-repair");
      btn.disabled = true;
      toast("正在恢复 GP-02 NMEA 配置…", "");
      await api("/api/repair", { method: "POST", body: "{}" });
      toast("修复序列已发送，等待数据恢复", "ok");
      setTimeout(() => { btn.disabled = false; }, 6000);
    } catch (e) {
      toast(e.message, "error");
      setTimeout(() => { $("#btn-repair").disabled = false; }, 3000);
    }
  }

  async function setSource(mode) {
    try {
      const r = await api("/api/source", { method: "POST", body: JSON.stringify({ mode }) });
      toast(r.message || "正在切换数据源", "ok");
    } catch (e) { toast(e.message, "error"); }
  }

  async function copyCoords() {
    if (!S || S.fix.lat === null) { toast("没有可复制的坐标", "error"); return; }
    const text = `${S.fix.lat.toFixed(7)}, ${S.fix.lon.toFixed(7)}`;
    try { await navigator.clipboard.writeText(text); toast(`已复制 ${text}`, "ok"); }
    catch (e) { toast(text, ""); }
  }

  function setTab(name) {
    if (!["list", "sky", "signal"].includes(name)) return;
    currentTab = name;
    $$("#sat-tabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    $$(".tab-view").forEach((v) => v.classList.remove("active"));
    $(`#view-${name}`).classList.add("active");
    if (name === "sky") setTimeout(renderSky, 30);
    if (name === "signal") setTimeout(renderSignal, 30);
  }

  // ------------------------------------------------------------------
  // events
  function bindEvents() {
    $$("#menubar .menu-item").forEach((el) => {
      el.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const name = el.dataset.menu;
        if (el.classList.contains("open")) { closeMenu(); return; }
        openMenu(name, el);
      });
      el.addEventListener("mouseenter", () => {
        if ($("#menu-layer").children.length) openMenu(el.dataset.menu, el);
      });
    });
    document.addEventListener("click", (ev) => {
      if (!ev.target.closest("#menu-layer") && !ev.target.closest("#menubar")) closeMenu();
    });
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") { closeMenu(); closeDialog(); }
      if (ev.ctrlKey || ev.metaKey || ev.altKey) {
        if (ev.altKey && ev.key === "F4") { ev.preventDefault(); confirmExit(); }
        return;
      }
      const tag = (ev.target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select") return;
      const k = ev.key.toLowerCase();
      if (k === "1") setTab("list");
      else if (k === "2") setTab("sky");
      else if (k === "3") setTab("signal");
      else if (k === "c") { setFollow(true); centerMap(true); }
      else if (k === "t") setTrail(!trailOn);
      else if (k === "a") setAccuracy(!accuracyOn);
      else if (k === "r") repairGps();
      else if (k === "d") openDiagnostics();
      else if (k === "m") openCacheDialog();
      else if (k === "o") setOffline(!offline);
      else if (k === "n") toggleTheme();
      else if (k === "s") openSettings();
      else if (k === "f1") { ev.preventDefault(); showHelp(); }
      else if (k === "+" || k === "=") map && map.zoomIn();
      else if (k === "-") map && map.zoomOut();
      else if (k === "f11") { ev.preventDefault(); toggleFullscreen(); }
    });

    $("#sat-tbody").addEventListener("click", (ev) => {
      const tr = ev.target.closest("tr[data-key]");
      if (tr) selectSat(tr.dataset.key);
    });
    $$("#sat-tabs .tab").forEach((tab) => {
      tab.addEventListener("click", () => setTab(tab.dataset.tab));
    });

    $("#btn-follow").addEventListener("click", () => setFollow(!follow));
    $("#btn-tool-follow").addEventListener("click", () => { setFollow(true); centerMap(true); });
    $("#btn-unfollow").addEventListener("click", () => setFollow(false));
    $("#btn-trail").addEventListener("click", () => setTrail(!trailOn));
    $("#btn-tool-trail").addEventListener("click", () => setTrail(!trailOn));
    $("#btn-offline").addEventListener("click", () => setOffline(!offline));
    $("#btn-prefetch").addEventListener("click", openCacheDialog);
    $("#btn-tool-cache").addEventListener("click", openCacheDialog);
    $("#btn-repair").addEventListener("click", repairGps);
    $("#btn-tool-repair").addEventListener("click", repairGps);
    $("#btn-diagnostics").addEventListener("click", openDiagnostics);
    $("#btn-settings")?.addEventListener("click", openSettings);
    $("#btn-tool-settings").addEventListener("click", openSettings);
    $("#btn-copy-coord").addEventListener("click", copyCoords);
    $("#btn-map-center").addEventListener("click", () => { setFollow(true); centerMap(true); });
    $("#btn-zoom-in").addEventListener("click", () => map && map.zoomIn());
    $("#btn-zoom-out").addEventListener("click", () => map && map.zoomOut());
    $("#win-close").addEventListener("click", confirmExit);
    $("#win-min").addEventListener("click", () => toast("此窗口在 kiosk 模式下不能最小化", ""));
    $("#win-max").addEventListener("click", toggleFullscreen);
    $("#app-window").addEventListener("dblclick", (ev) => {
      if (ev.target.closest(".title-bar")) toggleFullscreen();
    });
  }

  // ------------------------------------------------------------------
  // boot
  function boot() {
    bindEvents();
    initMap();
    connect();
    clockTimer = setInterval(renderClock, 1000);
    $("#btn-tool-follow").classList.add("active");
    $("#btn-follow").classList.add("active");
    $("#btn-trail").classList.add("active");
    $("#btn-tool-trail").classList.add("active");
    $("#map-lock").classList.add("show");
    setMessage("正在连接 GPS95 服务…");
    setTimeout(() => {
      if (!S) toast("等待后端数据…", "");
    }, 4000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
