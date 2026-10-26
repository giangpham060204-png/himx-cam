(() => {
  "use strict";

  const token = document.querySelector('meta[name="arm-token"]').content;
  const AXES = ["m1", "m2", "m3"];
  const $ = (id) => document.getElementById(id);
  const state = {
    status: null,
    builtFor: null,
    busy: false,
    polling: false,
    refreshTimer: null,
    actionError: "",
  };

  function unit(axis) {
    return axis.kind === "rotary" ? "deg" : "mm";
  }

  function number(value, axis) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) {
      return "—";
    }
    const digits = axis.kind === "rotary" ? 1 : 2;
    return Number(Number(value).toFixed(digits)).toString();
  }

  function percent(value, axis) {
    const span = axis.max - axis.min;
    if (!span || value === null || value === undefined) return null;
    return Math.max(0, Math.min(100, ((value - axis.min) / span) * 100));
  }

  function setBanner(message) {
    const banner = $("banner");
    banner.textContent = message || "";
    banner.hidden = !message;
  }

  function setBusy(busy) {
    state.busy = busy;
    document.querySelectorAll("button:not(#estop)").forEach((button) => {
      button.disabled = busy;
    });
    document.querySelectorAll("input").forEach((input) => {
      input.disabled = busy;
    });
    if (!busy && state.status) {
      applyDisabledState(state.status);
    }
  }

  async function api(path, options = {}) {
    const headers = {
      "Content-Type": "application/json",
      "X-Arm-Token": token,
      ...(options.headers || {}),
    };
    const response = await fetch(path, { ...options, headers });
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      throw new Error(`HTTP ${response.status}`);
    }
    if (!response.ok || !payload.ok) {
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    return payload;
  }

  function buildAxes(status) {
    const axes = $("axes");
    axes.innerHTML = AXES.map((name, index) => {
      const axis = status.axes[name];
      const suffix = unit(axis);
      const steps = axis.kind === "rotary" ? [10, 5, 1] : [10, 1, 0.1];
      const minus = steps
        .map(
          (step) =>
            `<button type="button" data-jog-axis="${name}" data-delta="${-step}" aria-label="Jog ${name} minus ${step} ${suffix}">−${step}</button>`
        )
        .join("");
      const plus = [...steps]
        .reverse()
        .map(
          (step) =>
            `<button type="button" data-jog-axis="${name}" data-delta="${step}" aria-label="Jog ${name} plus ${step} ${suffix}">+${step}</button>`
        )
        .join("");
      return `
        <article class="axis" data-axis="${name}">
          <header class="axis-head">
            <div class="axis-id">
              <span class="index">${String(index + 1).padStart(2, "0")}</span>
              <h2>${name.toUpperCase()}</h2>
              <span class="axis-kind">${axis.kind}</span>
            </div>
            <div class="axis-readout">
              <span class="position" data-position="${name}">—</span>
              <span class="unit">${suffix}</span>
            </div>
          </header>
          <div class="travel">
            <div class="travel-labels">
              <span data-min="${name}">${number(axis.min, axis)}</span>
              <span>soft travel</span>
              <span data-max="${name}">${number(axis.max, axis)}</span>
            </div>
            <div class="rail" role="img" aria-label="${name} travel">
              <span class="rail-home" data-home-marker="${name}" title="Home position"></span>
              <span class="rail-marker" data-marker="${name}"></span>
            </div>
          </div>
          <div class="axis-controls">
            <div class="jog-group" role="group" aria-label="Jog ${name}">${minus}${plus}</div>
            <form class="move-form" data-move-axis="${name}">
              <label>
                Move to
                <input name="position" type="number" step="any" min="${axis.min}" max="${axis.max}" inputmode="decimal" placeholder="${number(axis.home, axis)}">
              </label>
              <button type="submit">Move</button>
            </form>
            <div class="axis-state">
              <span class="tag tag-unhomed" data-homed="${name}">unhomed</span>
              <span class="limit" data-limit="${name}"></span>
            </div>
          </div>
        </article>`;
    }).join("");

    $("limit-list").innerHTML = AXES.map((name) => {
      const axis = status.axes[name];
      return `<div><dt>${name.toUpperCase()}</dt><dd>${number(axis.min, axis)} .. ${number(axis.max, axis)} ${unit(axis)}</dd></div>`;
    }).join("");

    AXES.forEach((name) => {
      const axis = status.axes[name];
      const home = percent(axis.home, axis);
      const marker = document.querySelector(`[data-home-marker="${name}"]`);
      if (home !== null) marker.style.left = `${home}%`;
    });
    state.builtFor = status.config_hash;
  }

  function applyDisabledState(status) {
    AXES.forEach((name) => {
      const axis = status.axes[name];
      const homed = Boolean(status.homed[name]);
      const canMove = status.connected && homed && !status.estopped;
      const article = document.querySelector(`[data-axis="${name}"]`);
      if (!article) return;
      article.querySelectorAll("[data-jog-axis]").forEach((button) => {
        button.disabled = state.busy || !canMove;
      });
      const form = article.querySelector(`[data-move-axis="${name}"]`);
      if (form) {
        form.querySelectorAll("button, input").forEach((element) => {
          element.disabled = state.busy || !canMove;
        });
      }
      const tag = article.querySelector(`[data-homed="${name}"]`);
      tag.textContent = homed ? "homed" : "unhomed";
      tag.className = `tag ${homed ? "tag-homed" : "tag-unhomed"}`;
      article.querySelector(`[data-limit="${name}"]`).textContent =
        `${number(axis.min, axis)} .. ${number(axis.max, axis)} ${unit(axis)}`;
    });
    document.querySelectorAll("[data-home]").forEach((button) => {
      button.disabled = state.busy || !status.connected || status.estopped;
    });
  }

  function render(status) {
    state.status = status;
    if (state.builtFor !== status.config_hash) {
      buildAxes(status);
    }

    const link = $("link-state");
    link.textContent = status.connected ? "connected" : "offline";
    link.className = `value ${status.connected ? "value-online" : "value-offline"}`;
    $("klipper-state").textContent = status.klippy_state || "—";
    $("klipper-state").className =
      status.klippy_state === "ready"
        ? "value value-online"
        : status.klippy_state === "shutdown"
          ? "value value-alarm"
          : "value";
    $("config-hash").textContent = status.config_hash || "—";
    $("footer-config").textContent = `config ${status.config_hash || "—"}`;
    $("connect").textContent = status.connected ? "Reconnect" : "Connect";
    document.body.dataset.estopped = status.estopped ? "true" : "false";

    AXES.forEach((name) => {
      const axis = status.axes[name];
      const position = status.positions[name];
      const readout = document.querySelector(`[data-position="${name}"]`);
      readout.textContent = number(position, axis);
      const marker = document.querySelector(`[data-marker="${name}"]`);
      const pct = percent(position, axis);
      marker.style.opacity = pct === null ? "0" : "1";
      if (pct !== null) marker.style.left = `${pct}%`;
    });

    applyDisabledState(status);

    if (Array.isArray(status.tip) && status.tip.length === 3) {
      $("tip-x").textContent = Number(status.tip[0]).toFixed(2);
      $("tip-y").textContent = Number(status.tip[1]).toFixed(2);
      $("tip-z").textContent = Number(status.tip[2]).toFixed(2);
      $("tip-note").textContent = "Device-frame tip, millimetres.";
    } else {
      $("tip-x").textContent = "—";
      $("tip-y").textContent = "—";
      $("tip-z").textContent = "—";
      $("tip-note").textContent = "All axes must be homed.";
    }

    const message =
      status.error ||
      state.actionError ||
      (status.estopped
        ? "Emergency stop latched. Restart Klipper and reconnect."
        : "");
    setBanner(message);
  }

  function scheduleRefresh(delay = 1000) {
    clearTimeout(state.refreshTimer);
    state.refreshTimer = setTimeout(refresh, delay);
  }

  async function refresh() {
    if (state.polling) return;
    state.polling = true;
    try {
      const payload = await api("/api/status");
      render(payload.status);
    } catch (error) {
      setBanner(error.message);
    } finally {
      state.polling = false;
      scheduleRefresh();
    }
  }

  async function runAction(label, request) {
    if (state.busy) return;
    state.actionError = "";
    setBusy(true);
    try {
      const payload = await request();
      $("last-action").textContent = label;
      render(payload.status);
      setBanner("");
    } catch (error) {
      state.actionError = error.message;
      setBanner(error.message);
    } finally {
      setBusy(false);
      scheduleRefresh(120);
    }
  }

  async function emergencyStop() {
    state.actionError = "";
    setBusy(true);
    try {
      const payload = await api("/api/estop", { method: "POST" });
      $("last-action").textContent = "Emergency stop sent. Restart Klipper and reconnect.";
      render(payload.status);
      setBanner("");
    } catch (error) {
      state.actionError = error.message;
      setBanner(error.message);
    } finally {
      setBusy(false);
      scheduleRefresh(120);
    }
  }

  $("connect").addEventListener("click", () => {
    runAction("Connected.", () => api("/api/connect", { method: "POST" }));
  });

  $("estop").addEventListener("click", emergencyStop);

  document.addEventListener("click", (event) => {
    const jog = event.target.closest("[data-jog-axis]");
    if (jog) {
      const axis = jog.dataset.jogAxis;
      const delta = Number(jog.dataset.delta);
      const name = axis.toUpperCase();
      runAction(
        `${name} ${delta > 0 ? "+" : ""}${delta}`,
        () =>
          api("/api/jog", {
            method: "POST",
            body: JSON.stringify({ axis, delta }),
          })
      );
      return;
    }

    const home = event.target.closest("[data-home]");
    if (home) {
      const axis = home.dataset.home;
      const label = axis === "all" ? "Home all." : `Home ${axis.toUpperCase()}.`;
      runAction(label, () =>
        api("/api/home", {
          method: "POST",
          body: JSON.stringify({ axis }),
        })
      );
    }
  });

  document.addEventListener("submit", (event) => {
    const form = event.target.closest("[data-move-axis]");
    if (!form) return;
    event.preventDefault();
    const axis = form.dataset.moveAxis;
    const input = form.querySelector('input[name="position"]');
    if (input.value.trim() === "") {
      setBanner("Enter a target position first.");
      return;
    }
    const position = Number(input.value);
    if (!Number.isFinite(position)) {
      setBanner("Target position must be a finite number.");
      return;
    }
    runAction(
      `${axis.toUpperCase()} move to ${position}.`,
      () =>
        api("/api/move", {
          method: "POST",
          body: JSON.stringify({ axis, position }),
        })
    );
  });

  refresh();
})();
